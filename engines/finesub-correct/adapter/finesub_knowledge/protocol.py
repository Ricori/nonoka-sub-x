"""The knowledge protocol's wire and codec, shared by the client and the server.

One `multiprocessing.connection` per client thread (a named pipe on Windows,
a Unix socket elsewhere), authenticated with a one-off key, carrying JSON
frames. Calls are synchronous; while one side waits for its answer it serves
whatever the other side asks in the meantime, so a server function may call
back into the client (a `count_tokens` it was handed) and the client may ask
the server again from inside that callback.

What crosses:

- Plain data by value: `None`, `bool`, `int`, `float`, `str`, `bytes`, lists,
  tuples (named ones too), dicts, sets, paths, enums, dates, classes (by name),
  exceptions (rebuilt as their own type, so `except AmbiguousName` still
  catches), `sqlite3.Row`.
- Dataclass instances by value from the server (they are results). From the
  client only frozen ones go by value: a mutable one stays where it is.
- Everything else by handle: the side that owns the object lends it, the other
  side gets a `Remote` whose attribute reads, method calls, `with`, iteration
  and the like run on the owner. A repository, a store, its connection, a
  write lock, a `HandleMap`, a callback: all handles. A handle returned to its
  owner is the original object again.

So the original code runs unchanged on the side it is called on: whatever it
does to an object it did not create goes back to the side that did.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import enum
import importlib
import itertools
import json
import pathlib
import sys
import threading
import traceback
import types
from typing import Any, Callable, Mapping

PROTOCOL_VERSION = 1


class ProtocolError(RuntimeError):
    """The two ends do not understand each other (a bug, not a knowledge error)."""


class RemoteError(RuntimeError):
    """An exception the other side raised whose type this side cannot rebuild."""


REGISTRATION_SUFFIX = ".run"


def registration_dir(knowledge_root: str | pathlib.PurePath) -> pathlib.Path:
    """Where knowledge processes serving `knowledge_root` say how to reach them.

    Beside the base, not in it, like its write lock (`<root>.lock`): nothing
    under the knowledge directory is a client's to read.
    """

    root = pathlib.Path(knowledge_root)
    return root.with_name(f"{root.name}{REGISTRATION_SUFFIX}")


def qualified(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def resolve(name: str) -> Any:
    """`module:Attr.attr` -> the object."""

    module, _, path = name.partition(":")
    found: Any = importlib.import_module(module)
    for part in path.split(".") if path else ():
        found = getattr(found, part)
    return found


# --- what this process lends ---------------------------------------------------------


class Exports:
    """Objects this process lends the other side, by handle id (process-wide).

    A handle stays lent while the other side holds a `Remote` for it: every
    time it is sent the count goes up, and the other side gives the counts
    back when its proxies die (`Remote.__del__`, carried on the next frame).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._objects: dict[int, list[Any]] = {}
        self._ids: dict[int, int] = {}
        self._counter = itertools.count(1)

    def lend(self, value: Any) -> int:
        with self._lock:
            hid = self._ids.get(id(value))
            if hid is None or self._objects.get(hid, [None])[0] is not value:
                hid = next(self._counter)
                self._objects[hid] = [value, 0]
                self._ids[id(value)] = hid
            self._objects[hid][1] += 1
            return hid

    def get(self, hid: int) -> Any:
        with self._lock:
            entry = self._objects.get(hid)
        if entry is None:
            raise ProtocolError(f"handle {hid} is not lent by this process")
        return entry[0]

    def release(self, returns: list[list[int]]) -> None:
        dropped = []
        with self._lock:
            for hid, count in returns:
                entry = self._objects.get(hid)
                if entry is None:
                    continue
                entry[1] -= count
                if entry[1] <= 0:
                    del self._objects[hid]
                    if self._ids.get(id(entry[0])) == hid:
                        del self._ids[id(entry[0])]
                    dropped.append(entry[0])
        # Dropped outside the lock: a finalizer may run here.
        dropped.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._objects)


EXPORTS = Exports()

#: The other side's handles this process holds, by (origin, id), and per origin
#: the counts to give back once their proxies are gone. An origin is one other
#: process: a client has one (its server); a server has one per client process
#: (a task's worker, an agent's MCP server), whose handle ids overlap.
_PROXIES: "dict[tuple[str, int], Remote]" = {}
_PROXIES_LOCK = threading.RLock()  # re-entrant: a proxy may be finalized while one is made
_RETURNS: dict[str, list[list[int]]] = {}


def _take_returns(origin: str) -> list[list[int]]:
    pending = _RETURNS.get(origin)
    taken = []
    while pending:
        try:
            taken.append(pending.pop())
        except IndexError:  # pragma: no cover - raced with another thread
            break
    return taken


# --- the proxy for the other side's objects ---------------------------------------------

_DUNDER_OPS: dict[str, Callable[..., Any]] = {
    "__bool__": bool, "__iter__": iter, "__next__": next, "__len__": len, "__repr__": repr,
}


class Remote:
    """An object the other side owns. Every use goes to the owner.

    `__class__` answers the owner's class when this side can import it, so
    `isinstance(proxy, Cls)` reads as it would on the owner (FineSub asks a
    token counter's `__self__` whether it is a `LocalGeminiTokenCounter`);
    `type(proxy)` stays `Remote`.
    """

    __slots__ = ("_origin", "_hid", "_class", "_lineage", "_receipts", "__weakref__")

    @property
    def __class__(self) -> type:  # type: ignore[override]
        for name in object.__getattribute__(self, "_lineage"):
            found = _class_named(name)
            if found is not None:
                return found
        return Remote

    def __init__(self, origin: str, hid: int, class_name: str, lineage: tuple[str, ...] = ()) -> None:
        object.__setattr__(self, "_origin", origin)
        object.__setattr__(self, "_hid", hid)
        object.__setattr__(self, "_class", class_name)
        #: The owner's class and its bases, most derived first: `__class__` is
        #: the first this side has, so a subclass only the owner has still
        #: reads as its FineSub base.
        object.__setattr__(self, "_lineage", lineage or (class_name,))
        object.__setattr__(self, "_receipts", 0)

    def __del__(self) -> None:
        try:
            key = (self._origin, self._hid)
            with _PROXIES_LOCK:
                if _PROXIES.get(key) is self:
                    del _PROXIES[key]
                _RETURNS.setdefault(self._origin, []).append([self._hid, self._receipts])
        except Exception:  # noqa: BLE001 - interpreter shutdown
            pass

    def _peer(self) -> "Peer":
        peer = current_peer()
        if peer.origin != self._origin:
            raise ProtocolError(f"{self._class} belongs to another process than this thread talks to")
        return peer

    def _method(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return self._peer().request({"op": "method", "h": self._hid, "name": name}, args, kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return self._peer().request({"op": "getattr", "h": self._hid, "name": name}, (), {})

    def __setattr__(self, name: str, value: Any) -> None:
        self._peer().request({"op": "setattr", "h": self._hid, "name": name}, (value,), {})

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._peer().request({"op": "invoke", "h": self._hid}, args, kwargs)

    def __enter__(self) -> Any:
        return self._method("__enter__")

    def __exit__(self, exc_type: Any, exc: Any, _traceback: Any) -> Any:
        # The traceback object does not cross; the owner sees the exception.
        return self._method("__exit__", exc_type, exc, None)

    def __iter__(self) -> Any:
        return self._method("__iter__")

    def __next__(self) -> Any:
        return self._method("__next__")

    def __len__(self) -> int:
        return self._method("__len__")

    def __bool__(self) -> bool:
        return self._method("__bool__")

    def __getitem__(self, key: Any) -> Any:
        return self._method("__getitem__", key)

    def __contains__(self, key: Any) -> bool:
        return self._method("__contains__", key)

    def __repr__(self) -> str:
        try:
            return self._method("__repr__")
        except Exception:  # noqa: BLE001 - a repr must not raise
            return f"<remote {self._class} #{self._hid}>"


class RemoteMethod:
    """A bound method of an object the other side owns; `__self__` is that object."""

    __slots__ = ("_owner", "_name")

    def __init__(self, owner: Remote, name: str) -> None:
        self._owner = owner
        self._name = name

    @property
    def __self__(self) -> Remote:
        return self._owner

    @property
    def __name__(self) -> str:
        return self._name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._owner._method(self._name, *args, **kwargs)

    def __repr__(self) -> str:
        return f"<remote method {self._owner._class}.{self._name}>"


def _class_named(name: str) -> type | None:
    """The class `module:qualname` if its module is already loaded here, else None.

    Only loaded modules: code that asks `isinstance(proxy, Cls)` has imported
    `Cls` already, and a name the other side sends must not make this process
    import anything.
    """

    module, _, path = name.partition(":")
    found: Any = sys.modules.get(module)
    for part in path.split(".") if found is not None and path else ():
        found = getattr(found, part, None)
    return found if isinstance(found, type) else None


def _proxy(origin: str, hid: int, class_name: str, lineage: tuple[str, ...] = ()) -> Remote:
    with _PROXIES_LOCK:
        proxy = _PROXIES.get((origin, hid))
        if proxy is None:
            proxy = Remote(origin, hid, class_name, lineage)
            _PROXIES[(origin, hid)] = proxy
        object.__setattr__(proxy, "_receipts", proxy._receipts + 1)
        return proxy


class _Row(tuple):
    """`sqlite3.Row` rebuilt: index by position or column name, `keys()`."""

    _keys: tuple[str, ...]

    def __new__(cls, keys: list[str], values: list[Any]) -> "_Row":
        row = super().__new__(cls, values)
        row._keys = tuple(keys)
        return row

    def keys(self) -> list[str]:
        return list(self._keys)

    def __getitem__(self, key: Any) -> Any:  # type: ignore[override]
        if isinstance(key, str):
            folded = key.lower()
            for index, name in enumerate(self._keys):
                if name.lower() == folded:
                    return tuple.__getitem__(self, index)
            raise IndexError("No item with that key")
        return tuple.__getitem__(self, key)


# --- the codec ----------------------------------------------------------------------------

_PATH_CLASSES = {cls.__name__: cls for cls in (
    pathlib.Path, pathlib.PurePath, pathlib.WindowsPath, pathlib.PosixPath,
    pathlib.PureWindowsPath, pathlib.PurePosixPath,
)}
_MAX_DEPTH = 200


class Codec:
    """Values to JSON and back for one connection.

    `side` is "server" or "client" (it decides what dataclasses do); `origin`
    names the process at the other end, whose handles this codec turns into
    proxies.
    """

    def __init__(self, side: str, origin: str) -> None:
        self.side = side
        self.origin = origin

    def encode(self, value: Any, depth: int = 0) -> Any:
        if depth > _MAX_DEPTH:
            raise ProtocolError("value nests too deeply to cross (a cycle?)")
        kind = type(value)
        if value is None or kind in (bool, int, float, str):
            return value
        down = depth + 1
        if isinstance(value, Remote):
            if value._origin != self.origin:
                raise ProtocolError(f"{value._class} belongs to another process than this connection")
            return {"$r": value._hid}
        if isinstance(value, enum.Enum):
            return {"$e": qualified(kind), "v": self.encode(value.value, down)}
        if kind is list:
            return [self.encode(item, down) for item in value]
        if kind is tuple:
            return {"$t": [self.encode(item, down) for item in value]}
        if isinstance(value, tuple) and hasattr(kind, "_fields"):
            return {"$nt": qualified(kind), "v": [self.encode(item, down) for item in value]}
        if kind is dict:
            return {"$d": [[self.encode(key, down), self.encode(item, down)] for key, item in value.items()]}
        if kind is set:
            return {"$s": [self.encode(item, down) for item in value]}
        if kind is frozenset:
            return {"$f": [self.encode(item, down) for item in value]}
        if kind is bytes:
            return {"$b": base64.b64encode(value).decode("ascii")}
        if isinstance(value, pathlib.PurePath) and kind.__name__ in _PATH_CLASSES:
            return {"$p": kind.__name__, "v": str(value)}
        if kind is datetime.datetime:
            return {"$dt": value.isoformat()}
        if kind is datetime.date:
            return {"$date": value.isoformat()}
        if kind.__module__ == "sqlite3" and kind.__name__ == "Row":
            return {"$row": [list(value.keys()), [self.encode(item, down) for item in value]]}
        if isinstance(value, type):
            return {"$c": qualified(value)}
        if isinstance(value, BaseException):
            return {"$x": self.encode_exception(value)}
        if isinstance(value, types.MethodType):
            # A bound method crosses as (its object, its name), so the other
            # side can still ask for its `__self__`.
            return {"$bm": [self.encode(value.__self__, down), value.__func__.__name__]}
        if dataclasses.is_dataclass(value) and (
            self.side == "server" or kind.__dataclass_params__.frozen  # type: ignore[attr-defined]
        ):
            return {"$dc": qualified(kind), "f": {
                field.name: self.encode(getattr(value, field.name), down) for field in dataclasses.fields(value)
            }}
        return {"$h": EXPORTS.lend(value), "c": qualified(kind),
                "mro": [qualified(base) for base in kind.__mro__[1:] if base is not object]}

    def decode(self, value: Any) -> Any:
        if value is None or type(value) in (bool, int, float, str):
            return value
        if type(value) is list:
            return [self.decode(item) for item in value]
        (tag, payload), = ((key, item) for key, item in value.items() if key.startswith("$"))
        if tag == "$r":
            return EXPORTS.get(payload)
        if tag == "$h":
            name = value.get("c", "")
            return _proxy(self.origin, payload, name, (name, *value.get("mro", ())))
        if tag == "$m":
            return RemoteMethod(_proxy(self.origin, payload[0], payload[2]), payload[1])
        if tag == "$bm":
            owner = self.decode(payload[0])
            return RemoteMethod(owner, payload[1]) if type(owner) is Remote else getattr(owner, payload[1])
        if tag == "$e":
            return resolve(payload)(self.decode(value["v"]))
        if tag == "$t":
            return tuple(self.decode(item) for item in payload)
        if tag == "$nt":
            return resolve(payload)(*(self.decode(item) for item in value["v"]))
        if tag == "$d":
            return {self.decode(key): self.decode(item) for key, item in payload}
        if tag == "$s":
            return {self.decode(item) for item in payload}
        if tag == "$f":
            return frozenset(self.decode(item) for item in payload)
        if tag == "$b":
            return base64.b64decode(payload)
        if tag == "$p":
            return _PATH_CLASSES[payload](value["v"])
        if tag == "$dt":
            return datetime.datetime.fromisoformat(payload)
        if tag == "$date":
            return datetime.date.fromisoformat(payload)
        if tag == "$row":
            return _Row(payload[0], [self.decode(item) for item in payload[1]])
        if tag == "$c":
            return resolve(payload)
        if tag == "$x":
            return self.decode_exception(payload)
        if tag == "$dc":
            cls = resolve(payload)
            instance = object.__new__(cls)
            for name, item in value["f"].items():
                object.__setattr__(instance, name, self.decode(item))
            return instance
        raise ProtocolError(f"unknown value tag {tag!r}")

    def encode_exception(self, error: BaseException) -> dict[str, Any]:
        state = {}
        for name, item in vars(error).items() if hasattr(error, "__dict__") else ():
            try:
                state[name] = self.encode(item)
            except Exception:  # noqa: BLE001 - what cannot cross is left behind
                continue
        try:
            args = self.encode(list(error.args))
        except Exception:  # noqa: BLE001
            args = [str(arg) for arg in error.args]
        return {
            "c": qualified(type(error)), "args": args, "state": state, "message": str(error),
            "trace": "".join(traceback.format_exception(type(error), error, error.__traceback__))[-6000:],
        }

    def decode_exception(self, payload: Mapping[str, Any]) -> BaseException:
        args = tuple(self.decode(payload.get("args") or []))
        try:
            cls = resolve(payload["c"])
            if not (isinstance(cls, type) and issubclass(cls, BaseException)):
                raise TypeError(payload["c"])
            try:
                error = cls.__new__(cls, *args)
            except Exception:  # noqa: BLE001 - a __new__ with its own signature
                error = cls.__new__(cls)
            error.args = args
            for name, item in (payload.get("state") or {}).items():
                try:
                    setattr(error, name, self.decode(item))
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001 - a type this side does not have
            error = RemoteError(f"{payload.get('c')}: {payload.get('message')}")
        error.__knowledge_remote_trace__ = payload.get("trace", "")  # type: ignore[attr-defined]
        return error


# --- one connection ----------------------------------------------------------------------

_LOCAL = threading.local()
_PEER_FACTORY: Callable[[], "Peer"] | None = None


def set_peer_factory(factory: Callable[[], "Peer"]) -> None:
    """How a thread of this process gets its connection when it has none (the client)."""

    global _PEER_FACTORY
    _PEER_FACTORY = factory


def bind_peer(peer: "Peer | None") -> None:
    _LOCAL.peer = peer


def current_peer() -> "Peer":
    peer = getattr(_LOCAL, "peer", None)
    if peer is None:
        if _PEER_FACTORY is None:
            raise ProtocolError("this thread has no knowledge connection")
        peer = _PEER_FACTORY()
        _LOCAL.peer = peer
    return peer


class Peer:
    """One end of one connection: sends requests, and serves the other end's while it waits.

    `resolve_call` names the functions the other side may call by name (the
    server's allow-list; the client allows none). `request_context` adds what
    every outgoing request carries; `run` wraps the execution of an incoming
    one; `on_response` sees every answer (the client replays reporter events).
    """

    def __init__(
        self,
        connection: Any,
        side: str,
        origin: str,
        *,
        resolve_call: Callable[[str], Callable[..., Any]] | None = None,
        request_context: Callable[[], dict[str, Any]] | None = None,
        run: Callable[[Mapping[str, Any], Callable[[], Any]], tuple[Any, dict[str, Any]]] | None = None,
        on_response: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.connection = connection
        self.origin = origin
        self.codec = Codec(side, origin)
        self.resolve_call = resolve_call
        self.request_context = request_context
        self.run = run
        self.on_response = on_response
        self.entered: list[int] = []
        self._ids = itertools.count(1)

    # -- frames
    def send(self, message: dict[str, Any]) -> None:
        returns = _take_returns(self.origin)
        if returns:
            message["rel"] = returns
        self.connection.send_bytes(json.dumps(message, ensure_ascii=False).encode("utf-8"))

    def receive(self) -> dict[str, Any]:
        message = json.loads(self.connection.recv_bytes().decode("utf-8"))
        returns = message.pop("rel", None)
        if returns:
            EXPORTS.release(returns)
        return message

    # -- asking
    def request(self, fields: Mapping[str, Any], args: tuple, kwargs: Mapping[str, Any]) -> Any:
        rid = next(self._ids)
        message = {"k": "req", "id": rid, **fields,
                   "a": self.codec.encode(list(args)), "kw": self.codec.encode(dict(kwargs))}
        if self.request_context is not None:
            message.update(self.request_context())
        self.send(message)
        while True:
            incoming = self.receive()
            if incoming.get("k") == "res":
                if incoming.get("id") != rid:
                    raise ProtocolError(f"answer {incoming.get('id')} arrived for request {rid}")
                if self.on_response is not None:
                    self.on_response(incoming)
                if "err" in incoming:
                    raise self.codec.decode_exception(incoming["err"])
                return self.codec.decode(incoming.get("ok"))
            if incoming.get("k") == "req":
                self.send(self.serve(incoming))
                continue
            raise ProtocolError(f"unexpected frame {incoming.get('k')!r}")

    # -- answering
    def serve(self, message: Mapping[str, Any]) -> dict[str, Any]:
        answer: dict[str, Any] = {"k": "res", "id": message.get("id")}
        try:
            def execute() -> Any:
                return self._execute(message)

            if self.run is not None:
                result, extra = self.run(message, execute)
                answer.update(extra)
            else:
                result = execute()
            answer["ok"] = result
        except BaseException as error:  # noqa: BLE001 - every failure goes back to the caller
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            answer.pop("ok", None)
            extra = getattr(error, "__knowledge_extra__", None)
            if extra:
                answer.update(extra)
            answer["err"] = self.codec.encode_exception(error)
        return answer

    def _execute(self, message: Mapping[str, Any]) -> Any:
        op = message.get("op")
        args = self.codec.decode(message.get("a") or [])
        kwargs = self.codec.decode(message.get("kw") or {"$d": []})
        if op == "call":
            if self.resolve_call is None:
                raise ProtocolError("this side calls nothing by name")
            return self.codec.encode(self.resolve_call(str(message["fn"]))(*args, **kwargs))
        target = EXPORTS.get(int(message["h"]))
        if op == "invoke":
            return self.codec.encode(target(*args, **kwargs))
        if op == "getattr":
            name = str(message["name"])
            value = getattr(target, name)
            if callable(value) and not isinstance(value, type) and getattr(value, "__self__", None) is target:
                # A bound method: called where it lives, so it is not lent separately.
                return {"$m": [EXPORTS.lend(target), name, qualified(type(target))]}
            return self.codec.encode(value)
        if op == "setattr":
            setattr(target, str(message["name"]), args[0])
            return None
        if op == "method":
            name = str(message["name"])
            if name in _DUNDER_OPS:
                return self.codec.encode(_DUNDER_OPS[name](target, *args))
            result = getattr(target, name)(*args, **kwargs)
            hid = int(message["h"])
            if name == "__enter__":
                self.entered.append(hid)
            elif name == "__exit__" and hid in self.entered:
                self.entered.remove(hid)
            return self.codec.encode(result)
        raise ProtocolError(f"unknown operation {op!r}")

    def exit_entered(self) -> None:
        """Leave every `with` the other side entered through this connection and never left."""

        while self.entered:
            hid = self.entered.pop()
            try:
                EXPORTS.get(hid).__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - best effort on a dropped connection
                pass
