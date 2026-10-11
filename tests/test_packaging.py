"""Packaging (P-17): the console script must work from a regular install.

The fast checks always run. The wheel build + clean-venv install is slow
and needs network access for the build backend, so it runs only when
HARNESS_PACKAGING_TESTS=1 (CI sets it).
"""

import os
import subprocess
import sys
import tomllib
import venv
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_entry_point_targets_a_module_inside_the_package():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["scripts"]["harness"] == "harness.cli:main"
    assert meta["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["harness"]
    assert (ROOT / "harness" / "cli.py").is_file()
    assert not (ROOT / "cli.py").exists(), "a top-level cli.py would not ship in the wheel"


def test_unused_dependencies_are_gone():
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    names = {d.split(">")[0].split("=")[0].split("<")[0].strip().lower() for d in deps}
    assert not names & {"gitpython", "jinja2"}


@pytest.mark.packaging
@pytest.mark.skipif(
    os.environ.get("HARNESS_PACKAGING_TESTS") != "1",
    reason="set HARNESS_PACKAGING_TESTS=1 to build a wheel and install it in a fresh venv",
)
def test_wheel_installs_into_clean_venv_and_cli_runs(tmp_path):
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), str(ROOT)],
        check=True,
    )
    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel) as zf:
        assert "harness/cli.py" in zf.namelist()

    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=True)
    bindir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    python = bindir / ("python.exe" if os.name == "nt" else "python")
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", str(wheel)], check=True)

    exe = bindir / ("harness.exe" if os.name == "nt" else "harness")
    out = subprocess.run(
        [str(exe), "--help"],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,  # not the checkout: nothing may import from the source tree
    )
    assert "Agent Harness" in out.stdout
    for command in ("setup", "new", "run", "resume", "import", "status"):
        assert command in out.stdout
