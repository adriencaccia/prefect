import ast
import importlib
import importlib.util
import inspect
import os
import runpy
import sys
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import ModuleType
from typing import Any, Callable, Dict, Iterable, NamedTuple, Optional, Union

import fsspec

from prefect.exceptions import ScriptError
from prefect.logging.loggers import get_logger
from prefect.utilities.filesystem import filename, is_local_path, tmpchdir

logger = get_logger(__name__)


def to_qualified_name(obj: Any) -> str:
    """
    Given an object, returns its fully-qualified name: a string that represents its
    Python import path.

    Args:
        obj (Any): an importable Python object

    Returns:
        str: the qualified name
    """
    if sys.version_info < (3, 10):
        # These attributes are only available in Python 3.10+
        if isinstance(obj, (classmethod, staticmethod)):
            obj = obj.__func__
    return obj.__module__ + "." + obj.__qualname__


def from_qualified_name(name: str) -> Any:
    """
    Import an object given a fully-qualified name.

    Args:
        name: The fully-qualified name of the object to import.

    Returns:
        the imported object

    Examples:
        >>> obj = from_qualified_name("random.randint")
        >>> import random
        >>> obj == random.randint
        True
    """
    # Try importing it first so we support "module" or "module.sub_module"
    try:
        module = importlib.import_module(name)
        return module
    except ImportError:
        # If no subitem was included raise the import error
        if "." not in name:
            raise

    # Otherwise, we'll try to load it as an attribute of a module
    mod_name, attr_name = name.rsplit(".", 1)
    module = importlib.import_module(mod_name)
    return getattr(module, attr_name)


def objects_from_script(path: str, text: Union[str, bytes] = None) -> Dict[str, Any]:
    """
    Run a python script and return all the global variables

    Supports remote paths by copying to a local temporary file.

    WARNING: The Python documentation does not recommend using runpy for this pattern.

    > Furthermore, any functions and classes defined by the executed code are not
    > guaranteed to work correctly after a runpy function has returned. If that
    > limitation is not acceptable for a given use case, importlib is likely to be a
    > more suitable choice than this module.

    The function `load_script_as_module` uses importlib instead and should be used
    instead for loading objects from scripts.

    Args:
        path: The path to the script to run
        text: Optionally, the text of the script. Skips loading the contents if given.

    Returns:
        A dictionary mapping variable name to value

    Raises:
        ScriptError: if the script raises an exception during execution
    """

    def run_script(run_path: str):
        # Cast to an absolute path before changing directories to ensure relative paths
        # are not broken
        abs_run_path = os.path.abspath(run_path)
        with tmpchdir(run_path):
            try:
                return runpy.run_path(abs_run_path)
            except Exception as exc:
                raise ScriptError(user_exc=exc, path=path) from exc

    if text:
        with NamedTemporaryFile(
            mode="wt" if isinstance(text, str) else "wb",
            prefix=f"run-{filename(path)}",
            suffix=".py",
        ) as tmpfile:
            tmpfile.write(text)
            tmpfile.flush()
            return run_script(tmpfile.name)

    else:
        if not is_local_path(path):
            # Remote paths need to be local to run
            with fsspec.open(path) as f:
                contents = f.read()
            return objects_from_script(path, contents)
        else:
            return run_script(path)


def load_script_as_module(path: str) -> ModuleType:
    """
    Execute a script at the given path.

    Sets the module name to `__prefect_loader__`.

    If an exception occurs during execution of the script, a
    `prefect.exceptions.ScriptError` is created to wrap the exception and raised.

    During the duration of this function call, `sys` is modified to support loading.
    These changes are reverted after completion, but this function is not thread safe
    and use of it in threaded contexts may result in undesirable behavior.

    See https://docs.python.org/3/library/importlib.html#importing-a-source-file-directly
    """
    # We will add the parent directory to search locations to support relative imports
    # during execution of the script
    if not path.endswith(".py"):
        raise ValueError(f"The provided path does not point to a python file: {path!r}")

    parent_path = str(Path(path).resolve().parent)
    working_directory = os.getcwd()

    spec = importlib.util.spec_from_file_location(
        "__prefect_loader__",
        path,
        # Support explicit relative imports i.e. `from .foo import bar`
        submodule_search_locations=[parent_path, working_directory],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["__prefect_loader__"] = module

    # Support implicit relative imports i.e. `from foo import bar`
    sys.path.insert(0, working_directory)
    sys.path.insert(0, parent_path)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ScriptError(user_exc=exc, path=path) from exc
    finally:
        sys.modules.pop("__prefect_loader__")
        sys.path.remove(parent_path)
        sys.path.remove(working_directory)

    return module


def load_module(module_name: str) -> ModuleType:
    """
    Import a module with support for relative imports within the module.
    """
    # Ensure relative imports within the imported module work if the user is in the
    # correct working directory
    working_directory = os.getcwd()
    sys.path.insert(0, working_directory)

    try:
        return importlib.import_module(module_name)
    finally:
        sys.path.remove(working_directory)


def import_object(import_path: str):
    """
    Load an object from an import path.

    Import paths can be formatted as one of:
    - module.object
    - module:object
    - /path/to/script.py:object

    This function is not thread safe as it modifies the 'sys' module during execution.
    """
    if ".py:" in import_path:
        script_path, object_name = import_path.rsplit(":", 1)
        module = load_script_as_module(script_path)
    else:
        if ":" in import_path:
            module_name, object_name = import_path.rsplit(":", 1)
        elif "." in import_path:
            module_name, object_name = import_path.rsplit(".", 1)
        else:
            raise ValueError(
                f"Invalid format for object import. Received {import_path!r}."
            )

        module = load_module(module_name)

    return getattr(module, object_name)


class DelayedImportErrorModule(ModuleType):
    """
    A fake module returned by `lazy_import` when the module cannot be found. When any
    of the module's attributes are accessed, we will throw a `ModuleNotFoundError`.

    Adapted from [lazy_loader][1]

    [1]: https://github.com/scientific-python/lazy_loader
    """

    def __init__(self, frame_data, help_message, *args, **kwargs):
        self.__frame_data = frame_data
        self.__help_message = (
            help_message or "Import errors for this module are only reported when used."
        )
        super().__init__(*args, **kwargs)

    def __getattr__(self, attr):
        if attr in ("__class__", "__file__", "__frame_data", "__help_message"):
            super().__getattr__(attr)
        else:
            fd = self.__frame_data
            raise ModuleNotFoundError(
                f"No module named '{fd['spec']}'\n\nThis module was originally imported"
                f" at:\n  File \"{fd['filename']}\", line {fd['lineno']}, in"
                f" {fd['function']}\n\n    {''.join(fd['code_context']).strip()}\n"
                + self.__help_message
            )


def lazy_import(
    name: str, error_on_import: bool = False, help_message: str = ""
) -> ModuleType:
    """
    Create a lazily-imported module to use in place of the module of the given name.
    Use this to retain module-level imports for libraries that we don't want to
    actually import until they are needed.

    Adapted from the [Python documentation][1] and [lazy_loader][2]

    [1]: https://docs.python.org/3/library/importlib.html#implementing-lazy-imports
    [2]: https://github.com/scientific-python/lazy_loader
    """

    try:
        return sys.modules[name]
    except KeyError:
        pass

    spec = importlib.util.find_spec(name)
    if spec is None:
        if error_on_import:
            raise ModuleNotFoundError(f"No module named '{name}'.\n{help_message}")
        else:
            try:
                parent = inspect.stack()[1]
                frame_data = {
                    "spec": name,
                    "filename": parent.filename,
                    "lineno": parent.lineno,
                    "function": parent.function,
                    "code_context": parent.code_context,
                }
                return DelayedImportErrorModule(
                    frame_data, help_message, "DelayedImportErrorModule"
                )
            finally:
                del parent

    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module

    loader = importlib.util.LazyLoader(spec.loader)
    loader.exec_module(module)

    return module


class AliasedModuleDefinition(NamedTuple):
    """
    A definition for the `AliasedModuleFinder`.

    Args:
        alias: The import name to create
        real: The import name of the module to reference for the alias
        callback: A function to call when the alias module is loaded
    """

    alias: str
    real: str
    callback: Optional[Callable[[str], None]]


class AliasedModuleFinder(MetaPathFinder):
    def __init__(self, aliases: Iterable[AliasedModuleDefinition]):
        """
        See `AliasedModuleDefinition` for alias specification.

        Aliases apply to all modules nested within an alias.
        """
        self.aliases = aliases

    def find_spec(
        self,
        fullname: str,
        path=None,
        target=None,
    ) -> Optional[ModuleSpec]:
        """
        The fullname is the imported path, e.g. "foo.bar". If there is an alias "phi"
        for "foo" then on import of "phi.bar" we will find the spec for "foo.bar" and
        create a new spec for "phi.bar" that points to "foo.bar".
        """
        for alias, real, callback in self.aliases:
            if fullname.startswith(alias):
                # Retrieve the spec of the real module
                real_spec = importlib.util.find_spec(fullname.replace(alias, real, 1))
                # Create a new spec for the alias
                return ModuleSpec(
                    fullname,
                    AliasedModuleLoader(fullname, callback, real_spec),
                    origin=real_spec.origin,
                    is_package=real_spec.submodule_search_locations is not None,
                )


class AliasedModuleLoader(Loader):
    def __init__(
        self,
        alias: str,
        callback: Optional[Callable[[str], None]],
        real_spec: ModuleSpec,
    ):
        self.alias = alias
        self.callback = callback
        self.real_spec = real_spec

    def exec_module(self, _: ModuleType) -> None:
        root_module = importlib.import_module(self.real_spec.name)
        if self.callback is not None:
            self.callback(self.alias)
        sys.modules[self.alias] = root_module


def safe_load_namespace(source_code: str):
    parsed_code = ast.parse(source_code)

    namespace = {}

    # Walk through the AST and find all import statements
    for node in ast.walk(parsed_code):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name
                as_name = alias.asname if alias.asname else module_name
                try:
                    # Attempt to import the module
                    namespace[as_name] = importlib.import_module(module_name)
                    logger.debug("Successfully imported %s", module_name)
                except ImportError as e:
                    logger.debug(f"Failed to import {module_name}: {e}")
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module
            if module_name is None:
                continue
            try:
                module = importlib.import_module(module_name)
                for alias in node.names:
                    name = alias.name
                    asname = alias.asname if alias.asname else name
                    try:
                        # Get the specific attribute from the module
                        attribute = getattr(module, name)
                        namespace[asname] = attribute
                    except AttributeError as e:
                        logger.debug(
                            "Failed to retrieve %s from %s: %s", name, module_name, e
                        )
            except ImportError as e:
                logger.debug("Failed to import from %s: %s", node.module, e)

    # Handle local class definitions
    for node in ast.walk(parsed_code):
        if isinstance(node, ast.ClassDef):
            try:
                # Compile and evaluate each class and function definition locally
                code = compile(ast.Module(body=[node]), filename="<ast>", mode="exec")
                exec(code, namespace)
            except Exception as e:
                logger.debug("Failed to compile class definition: %s", e)
    return namespace
