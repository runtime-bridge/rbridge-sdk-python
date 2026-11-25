<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/runtime-bridge/rbridge/main/docs/assets/logo-dark.png">
    <img src="https://raw.githubusercontent.com/runtime-bridge/rbridge/main/docs/assets/logo-light.png" alt="rbridge" width="220">
  </picture>
</p>

# rbridge for Python

Python client for the [rbridge](https://github.com/runtime-bridge/rbridge) daemon. It lets a Python program hand tasks to the agent runtimes installed on the machine (Claude Code, OpenCode, any ACP agent) through one interface, with skills, approvals and streamed events.

Standard library only. Python 3.10 or later.

Documentation and examples: <https://runtime-bridge.github.io/docs/sdks/>.

## Install

Two ways, depending on where you want rbridge to live:

| | Command | rbridge lives | Its data lives | Removed by |
|---|---|---|---|---|
| **With the SDK** (per project) | `pip install rbridge` inside the project's virtual environment | Inside the environment, as an `rbridge` command in `.venv/bin` | The project's `.rbridge` folder | `pip uninstall rbridge`, or deleting the environment; delete `.rbridge` for the data |
| **On its own** (whole machine) | rbridge's [install script](https://github.com/runtime-bridge/rbridge#install), then `pip install rbridge` | On your `PATH`, for every project | Your user folders | `rbridge uninstall --yes` (add `--purge` for the data) |

The wheels for Linux, macOS and Windows (x86-64 and ARM) carry the rbridge program, so the first way needs nothing else. On any other platform pip installs a pure-Python wheel without it, and the SDK uses an rbridge installed on its own.

With rbridge inside the environment, `RBRIDGE_HOME` (when set) still decides where the data goes. Otherwise the project is the folder holding the virtual environment, or the nearest folder upwards with `.rbridge`, `pyproject.toml`, `setup.py`, `requirements.txt` or `.git`. Outside a virtual environment the install counts as global and uses your user folders.

Until the packages are published, install from Git (the client only; rbridge on its own), or build the wheels yourself: `make cross` in the rbridge repository, then `python3 scripts/build_wheels.py --binaries ../rbridge/bin` here, and `pip install dist/<the wheel for your platform>`.

## Use

```python
import rbridge

with rbridge.connect(client="my-app") as rb:
    result = rb.run(
        {"workspace": "/home/me/project", "prompt": "Review this project for security issues", "skills": ["security-audit"]},
        on_event=lambda e: e["type"] == "message.delta" and print(e["data"]["text"], end=""),
        approver=lambda req: req.get("kind") == "read",  # anything else is denied
    )
    print(result["output"])
```

`connect()` starts a daemon when none is running, using the bundled rbridge (or `RBRIDGE_BIN`, or rbridge on `PATH`); a daemon started this way stops by itself after 30 idle minutes. Pass `start=False` to only connect to one already running. Tasks can be a plain prompt (`rb.run("Summarise README.md")`), and the workspace defaults to the current folder. `connect()` uses the daemon's Unix socket, found the same way rbridge finds it (`RBRIDGE_HOME`, XDG and OS defaults); pass `socket_path` to override. `connect_http()` uses the local HTTP API instead and reads the URL and token from the daemon's runtime directory; the daemon must have `daemon.http.enabled: true`.

| Call | What it does |
|---|---|
| `run(task, on_event=, approver=, model=)` | New session, one turn, close |
| `open(task, ...)` / `resume(id, ...)` | `Session` with `prompt()`, `cancel()`, `close()`; usable with `with` |
| `providers()`, `skills()`, `sessions()`, `status()` | Read the daemon's state |
| `add_skill(path)`, `set_skill_trust(id, trust)` | Add a skill your application ships and mark it reviewed |
| `add_skills(dirs, only=, exclude=, trust=, dry_run=)` | Add every skill found in one or more folders (or a Git repository), with filters |
| `search_skills(query)`, `add_catalog_skill(ref)` | Use configured skill catalogs |

`on_event` runs on a background thread for every event. `approver` answers permission requests the policy profile leaves to the caller; return `True`, `False` or `{"allow": ..., "option_id": ..., "remember": ...}`, where `remember` allows the same kind of request for the rest of the session. Without an approver such requests are denied. Errors are `rbridge.RbridgeError` with rbridge's stable `code`.

`on_input` answers questions the runtime asks the user, as a form (`request["schema"]`) or a page to open (`request["url"]`, which rbridge never opens): return `{"action": "accept", "content": {...}}`, `{"action": "decline"}` or `{"action": "cancel"}`. Without it such questions are declined.

ACP runtimes that offer session modes and settings expose them on the session; Claude Code does not, as its permission modes could bypass policy:

```python
with client.open({"workspace": "/path/to/project"}, mode="code") as s:
    print(s.modes())                  # {"current": "code", "modes": [...]}
    s.set_option("model", "large")
```

Sessions are visible only to the `client` name that opened them.

## Develop

Tests start a real daemon whose only runtime is rbridge's fake ACP agent. Build both in a sibling rbridge checkout (`make build fakeagent`), or point `RBRIDGE_BIN` and `RBRIDGE_FAKE_AGENT` at them, then:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

## License

Apache 2.0.
