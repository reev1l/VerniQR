"""Install missing dependencies for a source ZIP without changing saved settings."""

import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK_DEPENDENCIES = """
import importlib.metadata
import pathlib
import sys
try:
    for line in pathlib.Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        name, expected = line.strip().split('==', 1)
        if importlib.metadata.version(name) != expected:
            raise ValueError('Version mismatch')
except (importlib.metadata.PackageNotFoundError, ValueError):
    sys.exit(1)
"""


def main():
    if sys.version_info < (3, 11):  # noqa: UP036 - bootstrap also runs before Python is validated.
        print("Python 3.11 or newer is required. Install it from python.org.")
        return 1
    environment = ROOT / ".venv"
    python = environment / "Scripts" / "python.exe"
    if not python.is_file():
        print("Creating the local Python environment...", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    requirements = ROOT / "requirements.lock"
    checked = subprocess.run(
        [str(python), "-c", CHECK_DEPENDENCIES, str(requirements)],
        cwd=ROOT,
        check=False,
    )
    if checked.returncode:
        print(
            "Installing dependencies. Internet access is required on the first start...", flush=True
        )
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(requirements),
            ],
            cwd=ROOT,
            check=True,
        )
    print("Dependencies are ready.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Installation failed ({type(error).__name__}). Check the message above.")
        sys.exit(1)
