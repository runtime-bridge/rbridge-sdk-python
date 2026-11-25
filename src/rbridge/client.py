"""Client for the rbridge daemon over IPC (Unix socket) or local HTTP."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

PROTOCOL_VERSION = 1

Prompt = Union[str, List[Dict[str, Any]]]
Event = Dict[str, Any]
Approver = Callable[[Dict[str, Any]], Union[bool, Dict[str, Any]]]
# Answers a runtime question with {"action": "accept" | "decline" | "cancel", "content": {...}}.
InputHandler = Callable[[Dict[str, Any]], Dict[str, Any]]


class RbridgeError(Exception):
    """An error carrying one of rbridge's stable error codes."""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def text(s: str) -> List[Dict[str, Any]]:
    """A prompt made of one text block."""
    return [{"type": "text", "text": s}]


def _prompt(p: Prompt) -> List[Dict[str, Any]]:
    return text(p) if isinstance(p, str) else p


def _task(task: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
    """A task with the conveniences filled in: a plain string is the prompt,
    the workspace defaults to the current folder, and a relative workspace is
    made absolute (the daemon runs elsewhere and needs a full path)."""
    t = {"prompt": task} if isinstance(task, str) else dict(task)
    t["workspace"] = os.path.abspath(t.get("workspace") or os.getcwd())
    return t


def runtime_dir(env: Optional[Dict[str, str]] = None) -> str:
    """The daemon's runtime directory, following rbridge's own rules."""
    env = os.environ if env is None else env
    if env.get("RBRIDGE_HOME"):
        return os.path.join(env["RBRIDGE_HOME"], "run")
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        local = env.get("LocalAppData") or os.path.join(home, "AppData", "Local")
        return os.path.join(local, "rbridge", "run")
    if sys.platform == "darwin":
        return os.path.join(env.get("TMPDIR") or "/tmp", f"rbridge-{os.getuid()}")
    xdg_run = env.get("XDG_RUNTIME_DIR")
    if xdg_run and os.path.isabs(xdg_run):
        return os.path.join(xdg_run, "rbridge")
    data = env.get("XDG_DATA_HOME")
    if not (data and os.path.isabs(data)):
        data = os.path.join(home, ".local", "share")
    return os.path.join(data, "rbridge", "run")


class _IPC:
    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(path)
        except OSError as e:
            self.sock.close()
            raise RbridgeError("provider_unavailable", f"no rbridge daemon at {path}: {e}") from None
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.next_id = 0
        self.pending: Dict[int, Dict[str, Any]] = {}
        self.listeners: Dict[str, Callable[[Event], None]] = {}
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        f = self.sock.makefile("r", encoding="utf-8")
        try:
            for line in f:
                if line.strip():
                    self._dispatch(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                self.closed = True
                for slot in self.pending.values():
                    slot["error"] = RbridgeError("provider_unavailable", "connection to the daemon closed")
                    slot["done"].set()
                self.pending.clear()

    def _dispatch(self, m: Dict[str, Any]) -> None:
        if m.get("method") == "session.event":
            ev = m["params"]["event"]
            cb = self.listeners.get(ev.get("session_id"))
            if cb:
                cb(ev)
            return
        if "method" in m:
            return
        with self.lock:
            slot = self.pending.pop(m.get("id"), None)
        if slot is None:
            return
        if "error" in m:
            d = m["error"].get("data") or {}
            slot["error"] = RbridgeError(d.get("code", "internal_error"), d.get("message", m["error"].get("message", "")), d.get("details"))
        else:
            slot["result"] = m.get("result")
        slot["done"].set()

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        with self.lock:
            if self.closed:
                raise RbridgeError("provider_unavailable", "connection to the daemon closed")
            self.next_id += 1
            mid = self.next_id
            slot: Dict[str, Any] = {"done": threading.Event()}
            self.pending[mid] = slot
        data = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}) + "\n"
        with self.write_lock:
            self.sock.sendall(data.encode("utf-8"))
        slot["done"].wait()
        if "error" in slot:
            raise slot["error"]
        return slot.get("result")

    def subscribe(self, session_id: str, on_event: Callable[[Event], None]) -> Callable[[], None]:
        self.listeners[session_id] = on_event
        self.call("sessions.subscribe", {"id": session_id, "after": 0})

        def stop() -> None:
            self.listeners.pop(session_id, None)
            try:
                self.call("sessions.unsubscribe", {"id": session_id})
            except RbridgeError:
                pass

        return stop

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class _HTTP:
    def __init__(self, url: str, token: str, client: str):
        self.url = url.rstrip("/")
        self.token = token
        self.client = client

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "X-Rbridge-Client": self.client, "Content-Type": "application/json"}

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error", {})
            except ValueError:
                err = {}
            raise RbridgeError(err.get("code", "internal_error"), err.get("message", str(e)), err.get("details")) from None
        except urllib.error.URLError as e:
            raise RbridgeError("provider_unavailable", f"cannot reach the daemon at {self.url}: {e.reason}") from None

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        p = params or {}
        sid = urllib.parse.quote(str(p.get("id", "")), safe="")
        routes = {
            "providers.list": ("GET", "/v1/providers" + ("?refresh=true" if p.get("refresh") else ""), None),
            "skills.list": ("GET", "/v1/skills", None),
            "skills.add": ("POST", "/v1/skills", p),
            "skills.add_many": ("POST", "/v1/skills/batch", p),
            "skills.trust": ("PUT", f"/v1/skills/{sid}/trust", {"trust": p.get("trust")}),
            "skills.search": ("GET", "/v1/skills/search?q=" + urllib.parse.quote(str(p.get("query", ""))), None),
            "sessions.list": ("GET", "/v1/sessions", None),
            "sessions.get": ("GET", f"/v1/sessions/{sid}", None),
            "sessions.open": ("POST", "/v1/sessions", p),
            "sessions.resume": ("POST", f"/v1/sessions/{sid}/resume", {"model": p.get("model"), "input": p.get("input", False)}),
            "sessions.modes": ("GET", f"/v1/sessions/{sid}/modes", None),
            "sessions.set_mode": ("PUT", f"/v1/sessions/{sid}/mode", {"mode": p.get("mode")}),
            "sessions.options": ("GET", f"/v1/sessions/{sid}/options", None),
            "sessions.set_option": ("PUT", "/v1/sessions/{}/options/{}".format(sid, urllib.parse.quote(str(p.get("option_id", "")), safe="")), {"value": p.get("value")}),
            "sessions.prompt": ("POST", f"/v1/sessions/{sid}/turns", {"prompt": p.get("prompt"), "wait": p.get("wait", True)}),
            "sessions.cancel": ("POST", f"/v1/sessions/{sid}/cancel", {}),
            "sessions.close": ("DELETE", f"/v1/sessions/{sid}", None),
            "daemon.status": ("GET", "/v1/status", None),
        }
        if method == "inputs.answer":
            path = "/v1/sessions/{}/inputs/{}".format(urllib.parse.quote(p["session_id"], safe=""), urllib.parse.quote(p["input_id"], safe=""))
            return self._request("POST", path, p)
        if method == "approvals.answer":
            path = "/v1/sessions/{}/approvals/{}".format(urllib.parse.quote(p["session_id"], safe=""), urllib.parse.quote(p["approval_id"], safe=""))
            return self._request("POST", path, p)
        if method not in routes:
            raise RbridgeError("invalid_input", f"method {method} is not available over HTTP")
        verb, path, body = routes[method]
        return self._request(verb, path, body)

    def subscribe(self, session_id: str, on_event: Callable[[Event], None]) -> Callable[[], None]:
        req = urllib.request.Request(f"{self.url}/v1/sessions/{urllib.parse.quote(session_id, safe='')}/events", headers=self._headers())
        try:
            resp = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            err = json.loads(e.read()).get("error", {})
            raise RbridgeError(err.get("code", "internal_error"), err.get("message", str(e))) from None
        stopped = threading.Event()

        def read() -> None:
            try:
                for raw in resp:
                    if stopped.is_set():
                        return
                    line = raw.decode("utf-8").rstrip("\n")
                    if line.startswith("data: "):
                        ev = json.loads(line[6:])
                        if ev.get("seq"):
                            on_event(ev)
            except (OSError, ValueError):
                pass

        threading.Thread(target=read, daemon=True).start()

        def stop() -> None:
            stopped.set()
            try:
                resp.close()
            except OSError:
                pass

        return stop

    def close(self) -> None:
        pass


class Session:
    """A multi-turn session with one runtime."""

    def __init__(self, transport: Any, record: Dict[str, Any]):
        self._t = transport
        self.record = record
        self.id: str = record["id"]
        self._unsubscribe: Optional[Callable[[], None]] = None

    def prompt(self, prompt: Prompt) -> Dict[str, Any]:
        return self._t.call("sessions.prompt", {"id": self.id, "prompt": _prompt(prompt)})

    def cancel(self) -> None:
        self._t.call("sessions.cancel", {"id": self.id})

    def modes(self) -> Dict[str, Any]:
        """Returns {"current": ..., "modes": [...]} for runtimes that offer modes."""
        return self._t.call("sessions.modes", {"id": self.id})

    def set_mode(self, mode: str) -> Dict[str, Any]:
        return self._t.call("sessions.set_mode", {"id": self.id, "mode": mode})

    def options(self) -> List[Dict[str, Any]]:
        """Returns the runtime's session settings, such as its model."""
        return self._t.call("sessions.options", {"id": self.id})

    def set_option(self, option_id: str, value: Union[str, bool]) -> List[Dict[str, Any]]:
        return self._t.call("sessions.set_option", {"id": self.id, "option_id": option_id, "value": value})

    def close(self) -> None:
        try:
            self._t.call("sessions.close", {"id": self.id})
        finally:
            if self._unsubscribe:
                self._unsubscribe()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class Client:
    """A daemon client. Its methods mirror the Go bridge.Client."""

    def __init__(self, transport: Any):
        self._t = transport

    def providers(self, refresh: bool = False) -> List[Dict[str, Any]]:
        return self._t.call("providers.list", {"refresh": refresh})

    def skills(self) -> List[Dict[str, Any]]:
        return self._t.call("skills.list")

    def add_skill(self, path: str, link: bool = False) -> Dict[str, Any]:
        """Adds a skill folder (an absolute path on this machine) to rbridge's
        library. New skills start unreviewed."""
        return self._t.call("skills.add", {"path": os.path.abspath(path), "link": link})

    def add_skills(self, source: Union[str, Sequence[str]], *, only: Optional[Sequence[str]] = None,
                   exclude: Optional[Sequence[str]] = None, trust: Optional[str] = None,
                   dry_run: bool = False, link: bool = False, rev: Optional[str] = None,
                   subdirs: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """Adds every skill found in one or more folders (each a skill, or a
        folder holding skills), or in a Git repository when source is a URL.

        only and exclude take names or patterns such as "metric-*"; trust is
        set on every skill added; dry_run finds and checks without installing.
        One skill failing does not stop the others: each item in the result has
        a status (added, updated, unchanged, would_add, skipped or failed)."""
        sources = [source] if isinstance(source, str) else list(source)
        params: Dict[str, Any] = {"only": list(only or []), "exclude": list(exclude or []),
                                  "trust": trust or "", "dry_run": dry_run, "link": link}
        if len(sources) == 1 and ("://" in sources[0] or sources[0].startswith("git@")):
            params.update(url=sources[0], rev=rev or "", subdirs=list(subdirs or []))
        else:
            params["paths"] = [os.path.abspath(s) for s in sources]
        return self._t.call("skills.add_many", params)

    def add_catalog_skill(self, ref: str) -> Dict[str, Any]:
        """Installs "catalog:id" or "id" from a configured skill catalog."""
        return self._t.call("skills.add", {"catalog": ref})

    def set_skill_trust(self, skill_id: str, trust: str) -> Dict[str, Any]:
        """Sets unreviewed, reviewed, trusted, restricted or blocked. Mark a
        skill reviewed only after reading it, or when your application ships it."""
        return self._t.call("skills.trust", {"id": skill_id, "trust": trust})

    def search_skills(self, query: str = "") -> Dict[str, Any]:
        """Searches the configured skill catalogs."""
        return self._t.call("skills.search", {"query": query})

    def sessions(self) -> List[Dict[str, Any]]:
        return self._t.call("sessions.list")

    def status(self) -> Dict[str, Any]:
        return self._t.call("daemon.status")

    def open(self, task: Union[str, Dict[str, Any]], on_event: Optional[Callable[[Event], None]] = None,
             approver: Optional[Approver] = None, model: Optional[str] = None,
             mode: Optional[str] = None, on_input: Optional[InputHandler] = None) -> Session:
        """Opens a session. Approval requests go to approver; without one they are denied.
        Questions the runtime asks go to on_input; without one they are declined.
        The workspace defaults to the current folder."""
        t = _task(task)
        if "prompt" in t:
            t["prompt"] = _prompt(t["prompt"])
        record = self._t.call("sessions.open", {"task": t, "model": model, "mode": mode, "input": on_input is not None})
        return self._attach(record, on_event, approver, on_input)

    def resume(self, session_id: str, on_event: Optional[Callable[[Event], None]] = None,
               approver: Optional[Approver] = None, model: Optional[str] = None,
               on_input: Optional[InputHandler] = None) -> Session:
        record = self._t.call("sessions.resume", {"id": session_id, "model": model, "input": on_input is not None})
        return self._attach(record, on_event, approver, on_input)

    def _attach(self, record: Dict[str, Any], on_event: Optional[Callable[[Event], None]], approver: Optional[Approver],
                on_input: Optional[InputHandler] = None) -> Session:
        session = Session(self._t, record)

        def handle(ev: Event) -> None:
            if on_event:
                on_event(ev)
            if ev.get("type") == "approval.required":
                threading.Thread(target=self._answer, args=(record["id"], ev.get("data") or {}, approver), daemon=True).start()
            if ev.get("type") == "input.required" and on_input is not None:
                threading.Thread(target=self._input, args=(record["id"], ev.get("data") or {}, on_input), daemon=True).start()

        session._unsubscribe = self._t.subscribe(record["id"], handle)
        return session

    def _answer(self, session_id: str, request: Dict[str, Any], approver: Optional[Approver]) -> None:
        allow, option_id, remember = False, None, False
        if approver is not None:
            try:
                d = approver(request)
                if isinstance(d, dict):
                    allow, option_id, remember = bool(d.get("allow")), d.get("option_id"), bool(d.get("remember"))
                else:
                    allow = d is True
            except Exception:
                allow = False
        try:
            self._t.call("approvals.answer", {"session_id": session_id, "approval_id": request.get("id"), "allow": allow, "option_id": option_id, "remember": remember})
        except RbridgeError:
            pass

    def _input(self, session_id: str, request: Dict[str, Any], on_input: InputHandler) -> None:
        try:
            answer = on_input(request)
        except Exception:
            answer = {"action": "decline"}
        if not isinstance(answer, dict):
            answer = {"action": "accept" if answer else "decline"}
        try:
            self._t.call("inputs.answer", {"session_id": session_id, "input_id": request.get("id"),
                                           "action": answer.get("action", "accept"), "content": answer.get("content")})
        except RbridgeError:
            pass

    def run(self, task: Union[str, Dict[str, Any]], on_event: Optional[Callable[[Event], None]] = None,
            approver: Optional[Approver] = None, model: Optional[str] = None,
            mode: Optional[str] = None, on_input: Optional[InputHandler] = None) -> Dict[str, Any]:
        """Runs a task in a new session and closes it. task may be just the
        prompt; the workspace defaults to the current folder."""
        t = _task(task)
        with self.open(t, on_event=on_event, approver=approver, model=model, mode=mode, on_input=on_input) as s:
            return s.prompt(t["prompt"])

    def close(self) -> None:
        self._t.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def socket_path(env: Optional[Dict[str, str]] = None) -> str:
    """The running daemon's socket. daemon.json records it, because the daemon
    moves the socket to a short temp path when the runtime directory is too
    deep for a Unix socket."""
    d = runtime_dir(env)
    try:
        with open(os.path.join(d, "daemon.json"), encoding="utf-8") as f:
            sock = json.load(f).get("socket")
        if sock:
            return sock
    except (OSError, ValueError):
        pass
    return os.path.join(d, "bridge.sock")


def connect(socket_path: Optional[str] = None, client: str = "default", env: Optional[Dict[str, str]] = None,
            start: bool = True) -> Client:
    """Connects to the daemon over its Unix socket.

    With rbridge bundled in this package and RBRIDGE_HOME unset, the daemon is
    this project's (its data in the project's .rbridge folder). A daemon that
    is not running is started first, using the bundled rbridge, RBRIDGE_BIN or
    rbridge on PATH; it stops by itself after 30 idle minutes. Pass
    start=False to only connect to one that is already running."""
    from .bundle import effective_env, start_daemon

    env = effective_env(env)
    if start and not socket_path:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(_socket_path(env))
        except OSError:
            start_daemon(env)
        finally:
            probe.close()
    path = socket_path or _socket_path(env)
    t = _IPC(path)
    t.call("initialize", {"client": client, "protocol_version": PROTOCOL_VERSION})
    return Client(t)


def connect_http(url: Optional[str] = None, token: Optional[str] = None, client: str = "default",
                 env: Optional[Dict[str, str]] = None) -> Client:
    """Connects over local HTTP. URL and token default to the running daemon's."""
    from .bundle import effective_env

    env = effective_env(env)
    if not url or not token:
        d = runtime_dir(env)
        with open(os.path.join(d, "daemon.json"), encoding="utf-8") as f:
            state = json.load(f)
        url = url or state.get("http")
        if not url:
            raise RbridgeError("provider_unavailable", "the daemon has no HTTP listener; set daemon.http.enabled")
        if not token:
            with open(os.path.join(d, "daemon.token"), encoding="utf-8") as f:
                token = f.read().strip()
    return Client(_HTTP(url, token, client))


_socket_path = socket_path
