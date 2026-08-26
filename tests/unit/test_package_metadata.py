import subprocess
import sys
from importlib import import_module
from importlib.metadata import version
from importlib.util import find_spec


def test_imported_version_matches_installed_distribution() -> None:
    """Catch package metadata drifting from the importable runtime version."""

    assert find_spec("firmquant") is not None, "firmquant must be importable"
    firmquant = import_module("firmquant")
    assert firmquant.__version__ == version("firmquant")


def test_module_entrypoint_reports_installed_version() -> None:
    """Catch a broken ``python -m firmquant`` package entrypoint."""

    completed = subprocess.run(
        [sys.executable, "-m", "firmquant", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "firmquant 0.1.0"
