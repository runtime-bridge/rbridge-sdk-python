"""The rbridge command a platform wheel installs into its environment.

It runs the bundled program, with RBRIDGE_HOME defaulted to the project's
.rbridge folder (see bundle.py), so typing rbridge inside the environment
uses this project's copy and data, never a global install.
"""

from __future__ import annotations

import os
import subprocess
import sys

from .bundle import bundled_binary, effective_env


def main() -> int:
    binary = bundled_binary()
    if not binary:
        sys.stderr.write("rbridge: this build of the package has no bundled rbridge; install rbridge itself\n")
        return 127
    env = effective_env()
    env["RBRIDGE_BUNDLED"] = "python:" + sys.prefix
    args = [binary] + sys.argv[1:]
    if os.name != "nt":
        os.execve(binary, args, env)
    return subprocess.run(args, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
