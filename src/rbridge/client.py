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
from typing import Any, Callable, Dict, List, Optional, Union

PROTOCOL_VERSION = 1

Prompt = Union[str, List[Dict[str, Any]]]
Event = Dict[str, Any]
Approver = Callable[[Dict[str, Any]], Union[bool, Dict[str, Any]]]


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
            "sessions.list": ("GET", "/v1/sessions", None),
            "sessions.get": ("GET", f"/v1/sessions/{sid}", None),
            "sessions.open": ("POST", "/v1/sessions", p),
            "sessions.resume": ("POST", f"/v1/sessions/{sid}/resume", {"model": p.get("model")}),
            "sessions.prompt": ("POST", f"/v1/sessions/{sid}/turns", {"prompt": p.get("prompt"), "wait": p.get("wait", True)}),
            "sessions.cancel": ("POST", f"/v1/sessions/{sid}/cancel", {}),
            "sessions.close": ("DELETE", f"/v1/sessions/{sid}", None),
            "daemon.status": ("GET", "/v1/status", None),
        }
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

    def sessions(self) -> List[Dict[str, Any]]:
        return self._t.call("sessions.list")

    def status(self) -> Dict[str, Any]:
        return self._t.call("daemon.status")

    def open(self, task: Dict[str, Any], on_event: Optional[Callable[[Event], None]] = None,
             approver: Optional[Approver] = None, model: Optional[str] = None) -> Session:
        """Opens a session. Approval requests go to approver; without one they are denied."""
        t = dict(task)
        if "prompt" in t:
            t["prompt"] = _prompt(t["prompt"])
        record = self._t.call("sessions.open", {"task": t, "model": model})
        return self._attach(record, on_event, approver)

    def resume(self, session_id: str, on_event: Optional[Callable[[Event], None]] = None,
               approver: Optional[Approver] = None, model: Optional[str] = None) -> Session:
        record = self._t.call("sessions.resume", {"id": session_id, "model": model})
        return self._attach(record, on_event, approver)

    def _attach(self, record: Dict[str, Any], on_event: Optional[Callable[[Event], None]], approver: Optional[Approver]) -> Session:
        session = Session(self._t, record)

        def handle(ev: Event) -> None:
            if on_event:
                on_event(ev)
            if ev.get("type") == "approval.required":
                threading.Thread(target=self._answer, args=(record["id"], ev.get("data") or {}, approver), daemon=True).start()

        session._unsubscribe = self._t.subscribe(record["id"], handle)
        return session

    def _answer(self, session_id: str, request: Dict[str, Any], approver: Optional[Approver]) -> None:
        allow, option_id = False, None
        if approver is not None:
            try:
                d = approver(request)
                if isinstance(d, dict):
                    allow, option_id = bool(d.get("allow")), d.get("option_id")
                else:
                    allow = d is True
            except Exception:
                allow = False
        try:
            self._t.call("approvals.answer", {"session_id": session_id, "approval_id": request.get("id"), "allow": allow, "option_id": option_id})
        except RbridgeError:
            pass

    def run(self, task: Dict[str, Any], on_event: Optional[Callable[[Event], None]] = None,
            approver: Optional[Approver] = None, model: Optional[str] = None) -> Dict[str, Any]:
        """Runs a task in a new session and closes it."""
        with self.open(task, on_event=on_event, approver=approver, model=model) as s:
            return s.prompt(task["prompt"])

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


def connect(socket_path: Optional[str] = None, client: str = "default", env: Optional[Dict[str, str]] = None) -> Client:
    """Connects to the daemon over its Unix socket."""
    path = socket_path or _socket_path(env)
    t = _IPC(path)
    t.call("initialize", {"client": client, "protocol_version": PROTOCOL_VERSION})
    return Client(t)


def connect_http(url: Optional[str] = None, token: Optional[str] = None, client: str = "default",
                 env: Optional[Dict[str, str]] = None) -> Client:
    """Connects over local HTTP. URL and token default to the running daemon's."""
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
