"""The everyday API: Rbridge and AsyncRbridge.

    from rbridge import Rbridge

    rb = Rbridge(runtime="claude-code")
    print(rb.run("Summarise README.md").text)

    rb.run("Explain app.py", stream=True)          # printed as it is written

    rb = Rbridge(runtime="claude-code", chat=True, stream=True)
    rb.run("What does app.py do?")                 # one conversation,
    rb.run("Now write tests for it")               # streamed, remembered
    rb.run(["Summarise it", "Shorter"])            # several messages, one call

    answers = rb.run_many(["Review a.py", "Review b.py"])

Everything below it (connections, sessions, events, approvals, threads) is
handled here. The lower-level Client and Session stay available through
Rbridge.client for anything this does not cover.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import queue
import random
import sys
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Optional, Sequence, Union

from .client import Client, RbridgeError, Session, _task, connect

Approve = Union[bool, str, Callable[[Dict[str, Any]], Any], None]
#: stream=True prints to the terminal; a function receives each chunk instead.
StreamTo = Union[bool, Callable[[str], Any], None]

_DONE = object()


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens,
                     round(self.cost_usd + other.cost_usd, 6))


@dataclass
class Response:
    """What a runtime answered."""

    text: str
    runtime: str
    session_id: str
    stop_reason: str
    #: The model that ran, as the runtime reported it (what latest or auto became).
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    #: The answer parsed as JSON when a schema was given, else None.
    data: Any = None
    #: With a Pydantic model as the schema, the answer as that model.
    parsed: Any = None
    #: The full result as the daemon sent it.
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __str__(self) -> str:
        return self.text

    @classmethod
    def _from(cls, raw: Dict[str, Any]) -> "Response":
        u = raw.get("usage") or {}
        data = raw.get("structured")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                pass
        return cls(text=raw.get("output", ""), runtime=raw.get("provider", ""), session_id=raw.get("session_id", ""),
                   stop_reason=raw.get("stop_reason", ""), model=raw.get("model", ""), data=data, raw=raw,
                   usage=Usage(int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0), float(u.get("cost_usd") or 0)))


class Stream:
    """The answer as it is written. Iterating yields text chunks; once it is
    done, .response holds the full Response. Every event the runtime sends
    (tools, plans, usage) is in .events as well."""

    def __init__(self, start: Callable[[Callable[[Dict[str, Any]], None]], Dict[str, Any]],
                 finish: Optional[Callable[[], None]] = None):
        self._q: "queue.Queue[Any]" = queue.Queue()
        self.response: Optional[Response] = None
        self.events: List[Dict[str, Any]] = []
        self._error: Optional[BaseException] = None
        self._finish = finish
        self._streamed = ""
        self._ended = False
        self._mu = threading.Lock()

        def on_event(ev: Dict[str, Any]) -> None:
            with self._mu:
                if self._ended:
                    return
                self.events.append(ev)
                if ev.get("type") == "message.delta":
                    chunk = (ev.get("data") or {}).get("text", "")
                    self._streamed += chunk
                    self._q.put(chunk)

        def work() -> None:
            try:
                self.response = Response._from(start(on_event))
                # Deltas can trail the result; whatever has not arrived yet is
                # taken from the result, so the chunks always add up to it.
                with self._mu:
                    self._ended = True
                    text = self.response.text
                    if text.startswith(self._streamed) and len(text) > len(self._streamed):
                        self._q.put(text[len(self._streamed):])
            except BaseException as exc:  # handed to the reader
                self._error = exc
            finally:
                with self._mu:
                    self._ended = True
                if self._finish:
                    try:
                        self._finish()
                    except Exception:
                        pass
                self._q.put(_DONE)

        threading.Thread(target=work, daemon=True).start()

    def __iter__(self) -> Iterator[str]:
        while True:
            item = self._q.get()
            if item is _DONE:
                break
            yield item
        if self._error:
            raise self._error

    def text(self) -> str:
        """Waits for the whole answer and returns it."""
        for _ in self:
            pass
        return self.response.text if self.response else ""


def _deliver(s: "Stream", stream: StreamTo) -> Response:
    """Hands every chunk to the terminal (stream=True) or to the given
    function, then returns the whole Response."""
    if callable(stream):
        for chunk in s:
            stream(chunk)
    else:
        for chunk in s:
            sys.stdout.write(chunk)
            sys.stdout.flush()
        if s.response and s.response.text and not s.response.text.endswith("\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
    assert s.response is not None
    return s.response


def _json_schema(schema: Any) -> Any:
    """A JSON schema from a dict, or from a Pydantic model class."""
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    return schema


def _parse(r: Response, schema: Any) -> Response:
    """With a Pydantic model as the schema, the answer as that model."""
    if r is not None and hasattr(schema, "model_validate") and r.data is not None:
        r.parsed = schema.model_validate(r.data)
    return r


def _stage(files: Sequence[Any]) -> "tuple[List[str], Callable[[], None]]":
    """Absolute paths for the files a run brings. Paths are used as they are;
    uploads (FastAPI's UploadFile, open files, or (name, bytes) pairs) are
    written to a temporary folder, removed once rbridge has copied them."""
    import shutil
    import tempfile

    paths: List[str] = []
    tmp: Optional[str] = None

    def spill(name: str, data: Any) -> str:
        nonlocal tmp
        if tmp is None:
            tmp = tempfile.mkdtemp(prefix="rbridge-upload-")
        dest = os.path.join(tmp, os.path.basename(name) or "upload")
        with open(dest, "wb") as f:
            if isinstance(data, (bytes, bytearray, memoryview)):
                f.write(data)
            else:
                shutil.copyfileobj(data, f)
        return dest

    for item in files or []:
        if isinstance(item, (str, os.PathLike)):
            paths.append(os.path.abspath(os.fspath(item)))
        elif isinstance(item, tuple) and len(item) == 2:
            paths.append(spill(item[0], item[1]))
        elif hasattr(item, "filename") and hasattr(item, "file"):  # FastAPI / Starlette UploadFile
            item.file.seek(0)
            paths.append(spill(item.filename, item.file))
        elif hasattr(item, "read") and hasattr(item, "name"):  # an open file
            paths.append(spill(os.path.basename(item.name), item))
        else:
            raise TypeError("files takes paths, (name, bytes) pairs, open files or uploaded files")
    return paths, (lambda: shutil.rmtree(tmp, ignore_errors=True)) if tmp else (lambda: None)


def _messages(prompt: Any) -> bool:
    return isinstance(prompt, (list, tuple)) and bool(prompt) and all(isinstance(p, str) for p in prompt)


def _approver(approve: Approve) -> Optional[Callable[[Dict[str, Any]], Any]]:
    if approve is None or approve is False or approve == "deny":
        return None
    if approve is True or approve == "allow":
        return lambda req: True
    if approve == "read":
        return lambda req: req.get("kind") in ("read", "search", "think")
    if callable(approve):
        return approve
    raise ValueError("approve must be True, False, 'allow', 'deny', 'read' or a function")


class Rbridge:
    """One place to run prompts on the agent runtimes installed here.

    Every argument is a default for the calls that follow, and each call can
    override it: files (paths, or uploads such as FastAPI's UploadFile; rbridge
    puts them in a working folder of their own and tells the runtime),
    runtime (an id from rb.runtimes(), such as "claude-code";
    None lets rbridge choose), model ("latest" for the runtime's newest
    flagship, "auto" for its own recommended choice, or a model name; unset
    uses the runtime's configured default), workspace (default: the current
    folder), skills, profile ("observe" for read-only, "standard",
    "controlled"), approve (what to do when the runtime asks permission:
    False to deny, True to allow, "read" for reads only, or a function),
    sandbox ("files" or "full"), timeout (seconds), max_concurrency for
    run_many, and max_cost_usd, after which no new run starts.

    stream=True prints every answer as it is written (or pass a function to
    receive the chunks); run still returns the whole Response. chat=True
    makes every run continue one conversation; rb.reset() starts a new one.

    The daemon is found or started on first use (see rbridge.connect).
    """

    def __init__(self, runtime: Optional[str] = None, *, model: Optional[str] = None,
                 workspace: Optional[str] = None, skills: Sequence[str] = (), profile: Optional[str] = None,
                 approve: Approve = False, sandbox: Optional[str] = None, timeout: Optional[float] = None,
                 max_concurrency: int = 4, max_cost_usd: Optional[float] = None, queue_timeout: Optional[float] = None,
                 stream: StreamTo = False,
                 chat: bool = False, app: str = "default", client: Optional[Client] = None,
                 env: Optional[Dict[str, str]] = None):
        self.defaults: Dict[str, Any] = dict(runtime=runtime, model=model, workspace=workspace, skills=list(skills),
                                             profile=profile, approve=approve, sandbox=sandbox, timeout=timeout,
                                             files=[])
        self.max_concurrency = max_concurrency
        #: Seconds a run may wait for rbridge to have room; None waits as long as it takes.
        self.queue_timeout = queue_timeout
        self.max_cost_usd = max_cost_usd
        #: Tokens and cost across every run made through this object.
        self.usage = Usage()
        self._app, self._env, self._client = app, env, client
        self._lock = threading.Lock()
        self.stream_to, self.chatting = stream, chat
        self._conversation: Optional["Chat"] = None
        # Conversations still open. A program that ends without close() must
        # not leave them in the daemon, holding its slots until they time out.
        self._chats: "weakref.WeakSet[Chat]" = weakref.WeakSet()
        atexit.register(_close_at_exit, weakref.ref(self))

    # ------------------------------------------------------------ plumbing

    @property
    def client(self) -> Client:
        """The lower-level Client, connected on first use."""
        with self._lock:
            if self._client is None:
                self._client = connect(client=self._app, env=self._env)
            return self._client

    def _options(self, overrides: Dict[str, Any]) -> Dict[str, Any]:
        unknown = set(overrides) - set(self.defaults) - {"schema"}
        if unknown:
            raise TypeError(f"unknown option {', '.join(sorted(unknown))}")
        o = {**self.defaults, **{k: v for k, v in overrides.items() if v is not None}}
        if self.max_cost_usd is not None and self.usage.cost_usd >= self.max_cost_usd:
            raise RbridgeError("budget_exceeded", f"spent ${self.usage.cost_usd:.4f} of the ${self.max_cost_usd} budget")
        return o

    def _task(self, prompt: Any, o: Dict[str, Any]) -> Dict[str, Any]:
        t = _task({"prompt": prompt, "workspace": o["workspace"]})
        if o["files"] and not o["workspace"]:
            # rbridge makes a working folder holding only the files.
            t.pop("workspace", None)
        if o["runtime"]:
            t["provider"] = o["runtime"]
        if o["skills"]:
            # A folder (of one skill or several) is added and trusted by rbridge.
            t["skills"] = [os.path.abspath(s) if os.path.isdir(s) else s for s in o["skills"]]
        if o["profile"]:
            t["profile"] = o["profile"]
        if o["sandbox"]:
            t["sandbox"] = o["sandbox"]
        if o.get("schema") is not None:
            t["output_schema"] = _json_schema(o["schema"])
        if o["timeout"]:
            t["timeout"] = int(o["timeout"] * 1e9)
        # Wait for a free session slot instead of failing when rbridge is busy.
        t["slot_wait"] = -1 if self.queue_timeout is None else max(1, int(self.queue_timeout * 1e9))
        return t

    def _count(self, r: Response) -> Response:
        with self._lock:
            self.usage = self.usage + r.usage
        return r

    def _open(self, t: Dict[str, Any], o: Dict[str, Any], on_event: Optional[Callable[[Dict[str, Any]], None]]) -> Session:
        """Opens a session, retrying while rbridge says the request may succeed
        later (rate limits, a daemon restarting). Nothing has run yet, so a
        retry repeats no work; a prompt is never retried."""
        give_up = None if self.queue_timeout is None else time.monotonic() + self.queue_timeout
        delay = 0.2
        paths, cleanup = _stage(o["files"])
        if paths:
            t = {**t, "files": paths}
        try:
            while True:
                try:
                    # rbridge copies the files when the session opens.
                    return self.client.open(t, on_event=on_event, approver=_approver(o["approve"]), model=o["model"])
                except RbridgeError as e:
                    if not e.retryable or (give_up is not None and time.monotonic() + delay > give_up):
                        raise
                time.sleep(delay + random.uniform(0, delay / 2))
                delay = min(delay * 2, 5.0)
        finally:
            cleanup()

    # --------------------------------------------------------------- calls

    def _stream(self, prompt: Any, o: Dict[str, Any], on_event: Optional[Callable[[Dict[str, Any]], None]]) -> "Stream":
        t = self._task(prompt, o)
        holder: Dict[str, Session] = {}

        def start(cb: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
            def both(ev: Dict[str, Any]) -> None:
                cb(ev)
                if on_event:
                    on_event(ev)
            holder["s"] = self._open(t, o, both)
            raw = holder["s"].prompt(t["prompt"])
            self._count(Response._from(raw))
            return raw

        return Stream(start, finish=lambda: holder["s"].close() if "s" in holder else None)

    def _default_chat(self, options: Dict[str, Any]) -> "Chat":
        with self._lock:
            if self._conversation is None:
                self._conversation = Chat(self, self._options(options))
            return self._conversation

    def run(self, prompt: Any, *, stream: StreamTo = None, chat: Optional[bool] = None,
            schema: Optional[Dict[str, Any]] = None, on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
            **options: Any) -> Any:
        """Runs a prompt and returns its Response.

        stream=True prints the answer as it is written (a function receives
        the chunks instead); the defaults come from Rbridge(stream=, chat=).
        With chat, the prompt continues this object's conversation, and a
        list of messages is sent in order, returning a list of Responses.
        schema asks for JSON matching it, parsed into Response.data; on_event
        sees every event as it happens."""
        stream = self.stream_to if stream is None else stream
        chat = self.chatting if chat is None else chat
        if _messages(prompt):
            if not chat:
                raise TypeError("a list of messages is a conversation: pass chat=True, or use run_many for separate prompts")
            return [self.run(p, stream=stream, chat=True, schema=schema, on_event=on_event, **options) for p in prompt]
        if chat:
            if schema is not None:
                options["schema"] = schema
            return self._default_chat(options).send(prompt, stream=stream)
        o = self._options({**options, "schema": schema})
        if stream:
            return _parse(_deliver(self._stream(prompt, o, on_event), stream), schema)
        t = self._task(prompt, o)
        with self._open(t, o, on_event) as s:
            return _parse(self._count(Response._from(s.prompt(t["prompt"]))), schema)

    def stream(self, prompt: Any, *, chat: Optional[bool] = None, schema: Optional[Dict[str, Any]] = None,
               on_event: Optional[Callable[[Dict[str, Any]], None]] = None, **options: Any) -> "Stream":
        """The answer as an iterator of text chunks, for when you want to
        handle each chunk yourself (stream=True on run does it for you)."""
        chat = self.chatting if chat is None else chat
        if chat:
            if schema is not None:
                options["schema"] = schema
            return self._default_chat(options).stream(prompt)
        return self._stream(prompt, self._options({**options, "schema": schema}), on_event)

    def reset(self) -> None:
        """Ends the conversation that chat=True keeps; the next run starts a new one."""
        with self._lock:
            conv, self._conversation = self._conversation, None
        if conv is not None:
            conv.close()

    def run_many(self, prompts: Sequence[Any], *, return_exceptions: bool = False,
                 max_concurrency: Optional[int] = None, **options: Any) -> List[Any]:
        """Runs several prompts at once and returns their Responses in order.

        Each item is a prompt, or a dict of a prompt and its own options
        ({"prompt": ..., "runtime": ..., "workspace": ...}). rbridge still
        enforces its own limits on sessions. With return_exceptions, a failed
        run gives its exception in its place instead of raising."""
        options.setdefault("chat", False)
        options.setdefault("stream", False)

        def one(item: Any) -> Any:
            if isinstance(item, dict):
                item = dict(item)
                return self.run(item.pop("prompt"), **{**options, **item})
            return self.run(item, **options)

        with ThreadPoolExecutor(max_workers=max(1, max_concurrency or self.max_concurrency)) as pool:
            futures = [pool.submit(one, p) for p in prompts]
            out = []
            for f in futures:
                try:
                    out.append(f.result())
                except Exception as exc:
                    if not return_exceptions:
                        raise
                    out.append(exc)
            return out

    def chat(self, **options: Any) -> "Chat":
        """A conversation with one runtime that remembers what was said."""
        return Chat(self, self._options(options))

    def runtimes(self) -> List[Dict[str, Any]]:
        """The runtimes rbridge knows, with whether each is installed."""
        return [{"id": p["id"], "name": p.get("display_name", p["id"]), "available": (p.get("runtime") or {}).get("health") == "available",
                 "version": (p.get("runtime") or {}).get("version"), "models": p.get("model_names") or {}}
                for p in self.client.providers()]

    def skills(self) -> List[Dict[str, Any]]:
        """The skills in rbridge's library: id, description, trust, version
        and whether it has scripts, for a program to show or choose from."""
        return [{"id": s["id"], "description": s.get("description", ""), "trust": s.get("trust"),
                 "version": s.get("version") or None, "scripts": bool(s.get("has_scripts"))}
                for s in self.client.skills()]

    def add_skills(self, path: Union[str, Sequence[str]], *, trust: str = "reviewed", **options: Any) -> List[str]:
        """Adds every skill in a folder (or several) and returns their ids.
        They are trusted as reviewed by default, for skills your program ships."""
        items = self.client.add_skills(path, trust=trust, **options)["items"]
        failed = [i for i in items if i["status"] == "failed"]
        if failed:
            raise RbridgeError("skill_invalid", "; ".join(f"{i.get('id')}: {i.get('reason')}" for i in failed))
        return [i["id"] for i in items if i["status"] != "skipped"]

    def close(self) -> None:
        self.reset()
        for chat in list(self._chats):
            chat.close()
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def __enter__(self) -> "Rbridge":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _close_at_exit(ref: "weakref.ReferenceType[Rbridge]") -> None:
    rb = ref()
    if rb is not None:
        try:
            rb.close()
        except Exception:  # the program is ending; a failed close must not raise there
            pass


class Chat:
    """A conversation: each send() continues the same session."""

    def __init__(self, rb: Rbridge, o: Dict[str, Any]):
        self._rb, self._o = rb, o
        rb._chats.add(self)
        self._session: Optional[Session] = None
        self._task: Optional[Dict[str, Any]] = None
        self._sink: Optional[Callable[[Dict[str, Any]], None]] = None
        self._turn = threading.Lock()
        self._seen: set = set()
        self._done: set = set()

    def _events(self, ev: Dict[str, Any]) -> None:
        # Events from a turn that has ended (they can trail its result) never
        # reach the next turn's stream.
        tid = ev.get("turn_id")
        if tid:
            if tid in self._done:
                return
            self._seen.add(tid)
        if self._sink:
            self._sink(ev)

    @property
    def id(self) -> Optional[str]:
        return self._session.id if self._session else None

    def _start(self, prompt: Any) -> Callable[[Optional[Callable[[Dict[str, Any]], None]]], Dict[str, Any]]:
        def start(sink: Optional[Callable[[Dict[str, Any]], None]]) -> Dict[str, Any]:
            with self._turn:
                self._sink = sink
                try:
                    if self._session is None:
                        self._task = self._rb._task(prompt, self._o)
                        self._session = self._rb._open(self._task, self._o, self._events)
                    raw = self._session.prompt(self._rb._task(prompt, self._o)["prompt"])
                finally:
                    self._sink = None
                    self._done |= self._seen
                self._rb._count(Response._from(raw))
                return raw
        return start

    def send(self, prompt: Any, *, stream: StreamTo = None) -> Any:
        """Sends a message and returns the Response; a list of messages is
        sent in order and returns a list. stream=True prints the answer as it
        is written (a function receives the chunks instead); the default
        comes from Rbridge(stream=)."""
        stream = self._rb.stream_to if stream is None else stream
        if _messages(prompt):
            return [self.send(p, stream=stream) for p in prompt]
        if stream:
            return _parse(_deliver(self.stream(prompt), stream), self._o.get("schema"))
        return _parse(Response._from(self._start(prompt)(None)), self._o.get("schema"))

    def stream(self, prompt: Any) -> "Stream":
        """The answer as an iterator of text chunks."""
        return Stream(self._start(prompt))

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> "Chat":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ----------------------------------------------------------------- async


async def _off(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Runs a blocking call on a thread of its own and waits for it.

    Not on asyncio's shared default pool: a call there can wait a long time
    for a free rbridge slot, and with enough of them waiting, the pool has no
    thread left for the call that would free a slot (a conversation's next
    turn, or its close). A thread that only waits costs next to nothing.
    """
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    def settle(ok: bool, value: Any) -> None:
        if not done.cancelled():
            done.set_result(value) if ok else done.set_exception(value)

    def work() -> None:
        try:
            value = fn(*args, **kwargs)
        except BaseException as exc:  # handed to the awaiting task
            loop.call_soon_threadsafe(settle, False, exc)
        else:
            loop.call_soon_threadsafe(settle, True, value)

    threading.Thread(target=work, name="rbridge-call", daemon=True).start()
    return await done


class AsyncStream:
    """Stream for async code: async for chunk in stream."""

    def __init__(self, stream: Stream):
        self._stream = stream

    @property
    def response(self) -> Optional[Response]:
        return self._stream.response

    @property
    def events(self) -> List[Dict[str, Any]]:
        return self._stream.events

    async def __aiter__(self) -> AsyncIterator[str]:
        it = iter(self._stream)
        while True:
            chunk = await _off(next, it, _DONE)
            if chunk is _DONE:
                return
            yield chunk

    async def text(self) -> str:
        return await _off(self._stream.text)


class AsyncChat:
    def __init__(self, chat: Chat):
        self._chat = chat

    @property
    def id(self) -> Optional[str]:
        return self._chat.id

    async def send(self, prompt: Any, *, stream: StreamTo = None) -> Any:
        return await _off(self._chat.send, prompt, stream=stream)

    def stream(self, prompt: Any) -> AsyncStream:
        return AsyncStream(self._chat.stream(prompt))

    async def close(self) -> None:
        await _off(self._chat.close)

    async def __aenter__(self) -> "AsyncChat":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


class AsyncRbridge:
    """Rbridge for asyncio: the same calls, awaited. Run prompts side by side
    with asyncio.gather."""

    def __init__(self, runtime: Optional[str] = None, **options: Any):
        self.sync = Rbridge(runtime, **options)

    @property
    def usage(self) -> Usage:
        return self.sync.usage

    async def run(self, prompt: Any, **options: Any) -> Any:
        """As Rbridge.run: stream=True prints as the answer is written, chat
        continues the conversation, a list of messages is sent in order."""
        return await _off(self.sync.run, prompt, **options)

    async def stream(self, prompt: Any, **options: Any) -> AsyncStream:
        """The answer as an async iterator of text chunks."""
        return AsyncStream(await _off(self.sync.stream, prompt, **options))

    async def reset(self) -> None:
        await _off(self.sync.reset)

    async def run_many(self, prompts: Sequence[Any], **options: Any) -> List[Any]:
        return await _off(self.sync.run_many, prompts, **options)

    def chat(self, **options: Any) -> AsyncChat:
        return AsyncChat(self.sync.chat(**options))

    async def runtimes(self) -> List[Dict[str, Any]]:
        return await _off(self.sync.runtimes)

    async def skills(self) -> List[Dict[str, Any]]:
        return await _off(self.sync.skills)

    async def add_skills(self, path: Union[str, Sequence[str]], **options: Any) -> List[str]:
        return await _off(self.sync.add_skills, path, **options)

    async def close(self) -> None:
        await _off(self.sync.close)

    async def __aenter__(self) -> "AsyncRbridge":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
