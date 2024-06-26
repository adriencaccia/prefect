import subprocess

from pytest_benchmark.fixture import BenchmarkFixture


def bench_prefect_help(benchmark: BenchmarkFixture):
    benchmark(subprocess.check_call, ["prefect", "--help"])


def bench_prefect_version(benchmark: BenchmarkFixture):
    benchmark(subprocess.check_call, ["prefect", "version"])


def bench_prefect_short_version(benchmark: BenchmarkFixture):
    benchmark(subprocess.check_call, ["prefect", "--version"])


def bench_prefect_profile_ls(benchmark: BenchmarkFixture):
    benchmark(subprocess.check_call, ["prefect", "profile", "ls"])
