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
from rbridge import Rbridge

rb = Rbridge(runtime="claude-code")

print(rb.run("Summarise README.md").text)
```

That is all the setup there is: `Rbridge()` finds rbridge (or the copy that came with this package), starts its daemon if it is not running, and the workspace is the current folder. `runtime` is any id from `rb.runtimes()`, such as `claude-code`, `opencode` or `codex`; leave it out and rbridge picks one.

**Stream** the answer as it is written. It is printed for you, and `run` still returns the whole answer:

```python
rb.run("Explain app.py", stream=True)
```

**Set it all once.** Options given to `Rbridge(...)` apply to every call. With `chat=True` each `run` continues the same conversation, and a list sends several messages in one call:

```python
rb = Rbridge(runtime="claude-code", chat=True, stream=True)

rb.run("What does app.py do?")
rb.run("Now write tests for it")
rb.run(["Summarise the tests", "Shorter"])
rb.reset()                                  # start a new conversation
```

Any call can still override a default (`rb.run("...", stream=False)`), and `rb.chat()` gives a separate conversation object when you want several at once (`chat.send(...)`).

To handle the chunks yourself, pass a function, `stream=my_function`, or take the iterator, `for chunk in rb.stream("...")`.

**Run several at once**, in order, without writing any threads:

```python
for r in rb.run_many(["Review a.py", "Review b.py", "Review c.py"]):
    print(r.text)
```

**Structured output**, parsed for you:

```python
r = rb.run("List the TODOs in this project", schema={"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "string"}}}})
print(r.data["todos"])
```

**Async** code uses `AsyncRbridge`, with the same calls awaited:

```python
from rbridge import AsyncRbridge

rb = AsyncRbridge(runtime="claude-code")
a, b = await asyncio.gather(rb.run("Review a.py"), rb.run("Review b.py"))
await rb.run("Explain app.py", stream=True)
async for chunk in await rb.stream("Explain app.py"):    # the iterator, if you want it
    print(chunk, end="")
```

### Options

Every option can be set on `Rbridge(...)` as a default and overridden on any call:

| Option | Meaning |
|---|---|
| `runtime`, `model` | Which runtime, and which of its models |
| `workspace` | The folder the runtime works in; default the current folder |
| `skills` | Skill ids, or folders of skills your program ships (added and trusted on first use) |
| `profile` | `observe` (read-only), `standard` or `controlled` |
| `approve` | When the runtime asks permission: `False` deny (default), `True` allow, `"read"` reads only, or a function given the request |
| `sandbox` | `files` or `full`, on Linux |
| `timeout` | Seconds per run |
| `max_concurrency` | How many `run_many` runs go at once (rbridge still applies its own limits) |
| `max_cost_usd` | After this much is spent, no new run starts (`rb.usage` has the running total) |
| `stream` | `True` prints answers as they are written; a function receives the chunks instead |
| `chat` | `True` makes every `run` continue one conversation |

A `Response` has `text`, `data` (with a schema), `runtime`, `session_id`, `stop_reason`, `usage` and `raw`. The `Stream` from `rb.stream()` yields text; afterwards `stream.response` is the `Response` and `stream.events` every event (tools, plans, usage). Pass `on_event=` to `run` to see events as they happen. Errors are `rbridge.RbridgeError` with a stable `code` (the list is at https://runtime-bridge.github.io/docs/errors/); `budget_exceeded` means `max_cost_usd` is spent.

## Lower-level API

`rb.client` (or `rbridge.connect()`) is the client underneath, for sessions you drive yourself, approvals with options, questions the runtime asks, modes and the daemon's state:

```python
import rbridge

with rbridge.connect(client="my-app") as c:
    result = c.run(
        {"workspace": "/home/me/project", "prompt": "Review this project for security issues", "skills": ["security-audit"]},
        on_event=lambda e: e["type"] == "message.delta" and print(e["data"]["text"], end=""),
        approver=lambda req: req.get("kind") == "read",
    )
    print(result["output"])
```

`connect()` starts a daemon when none is running (one that stops by itself after 30 idle minutes); pass `start=False` to only connect to a running one. It uses the daemon's Unix socket, found the same way rbridge finds it (`RBRIDGE_HOME`, XDG and OS defaults); pass `socket_path` to override. `connect_http()` uses the local HTTP API instead and reads the URL and token from the daemon's runtime directory; the daemon must have `daemon.http.enabled: true`.

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
