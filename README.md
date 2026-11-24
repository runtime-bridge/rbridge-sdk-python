# rbridge for Python

Python client for the [rbridge](https://github.com/rbridge/rbridge) daemon. It lets a Python program hand tasks to the agent runtimes installed on the machine (Claude Code, OpenCode, any ACP agent) through one interface, with skills, approvals and streamed events.

Standard library only. Python 3.9 or later.

## Use

Start the daemon once (`rbridge daemon start`), then:

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

`connect()` uses the daemon's Unix socket, found the same way rbridge finds it (`RBRIDGE_HOME`, XDG and OS defaults); pass `socket_path` to override. `connect_http()` uses the local HTTP API instead and reads the URL and token from the daemon's runtime directory; the daemon must have `daemon.http.enabled: true`.

| Call | What it does |
|---|---|
| `run(task, on_event=, approver=, model=)` | New session, one turn, close |
| `open(task, ...)` / `resume(id, ...)` | `Session` with `prompt()`, `cancel()`, `close()`; usable with `with` |
| `providers()`, `skills()`, `sessions()`, `status()` | Read the daemon's state |

`on_event` runs on a background thread for every event. `approver` answers permission requests the policy profile leaves to the caller; return `True`, `False` or `{"allow": ..., "option_id": ...}`. Without an approver such requests are denied. Errors are `rbridge.RbridgeError` with rbridge's stable `code`.

Sessions are visible only to the `client` name that opened them.

## Develop

Tests start a real daemon whose only runtime is rbridge's fake ACP agent. Build both in a sibling rbridge checkout (`make build fakeagent`), or point `RBRIDGE_BIN` and `RBRIDGE_FAKE_AGENT` at them, then:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

## License

Apache 2.0.
