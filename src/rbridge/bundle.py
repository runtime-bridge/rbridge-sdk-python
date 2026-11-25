"""The rbridge program that ships inside this package, and where it keeps data.

Platform wheels carry the rbridge binary in ``rbridge/_bin``. Installed into a
virtual environment, that copy belongs to the project: it is used instead of
any rbridge on PATH, and its configuration, skills, sessions and audit log
live in the project's ``.rbridge`` folder. Removing the package (or the
environment) removes the program; deleting ``.rbridge`` removes the data.

Without a bundled binary (the pure wheel, for platforms with no build), the
SDK talks to whatever rbridge daemon the environment points at, as before.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Dict, Mapping, Optional

BIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bin")
PROJECT_MARKERS = (".rbridge", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", ".git")


def bundled_binary() -> Optional[str]:
    """The rbridge program inside this package, if this build has one."""
    path = os.path.join(BIN_DIR, "rbridge.exe" if os.name == "nt" else "rbridge")
    if not os.path.isfile(path):
        return None
    if os.name != "nt" and not os.access(path, os.X_OK):
        # Some installers drop the executable bit from wheel files.
        try:
            os.chmod(path, os.stat(path).st_mode | 0o111)
        except OSError:
            pass
    return path


def find_binary(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """RBRIDGE_BIN, else the bundled program, else rbridge on PATH."""
    env = os.environ if env is None else env
    if env.get("RBRIDGE_BIN"):
        return env["RBRIDGE_BIN"]
    return bundled_binary() or shutil.which("rbridge", path=env.get("PATH"))


def in_virtualenv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def project_dir(cwd: Optional[str] = None) -> Optional[str]:
    """The project this environment belongs to, or None outside a virtual environment.

    A virtual environment inside the folder being worked in (the usual .venv)
    makes that folder the project. Otherwise the nearest folder upwards with a
    project marker is, and failing that the current folder.
    """
    if not in_virtualenv():
        return None
    cwd = os.path.abspath(cwd or os.getcwd())
    parent = os.path.dirname(os.path.abspath(sys.prefix))
    if cwd == parent or cwd.startswith(parent.rstrip(os.sep) + os.sep):
        return parent
    d = cwd
    while True:
        if any(os.path.exists(os.path.join(d, m)) for m in PROJECT_MARKERS):
            return d
        up = os.path.dirname(d)
        if up == d:
            return cwd
        d = up


def project_home(cwd: Optional[str] = None) -> Optional[str]:
    """The .rbridge folder a bundled rbridge uses when RBRIDGE_HOME is not set."""
    if not bundled_binary():
        return None
    d = project_dir(cwd)
    return os.path.join(d, ".rbridge") if d else None


def effective_env(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment with RBRIDGE_HOME defaulted to the project home when
    rbridge is bundled here and nothing else was chosen."""
    out = dict(os.environ if env is None else env)
    if not out.get("RBRIDGE_HOME"):
        home = project_home()
        if home:
            out["RBRIDGE_HOME"] = home
    return out


def start_daemon(env: Optional[Mapping[str, str]] = None) -> None:
    """Starts the daemon for this environment's rbridge home, if none is running."""
    from .client import RbridgeError

    env = effective_env(env)
    binary = find_binary(env)
    if not binary:
        raise RbridgeError("provider_unavailable",
                           "rbridge is not installed: install a platform build of this package, or rbridge itself "
                           "(https://github.com/runtime-bridge/rbridge), or set RBRIDGE_BIN")
    run = subprocess.run([binary, "daemon", "start"], env=env, capture_output=True, text=True, timeout=60)
    if run.returncode != 0 and "already running" not in (run.stdout + run.stderr):
        raise RbridgeError("provider_unavailable", f"could not start the rbridge daemon: {(run.stderr or run.stdout).strip()}")
