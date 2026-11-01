"""The wire to AiAudioHub: newline-delimited JSON-RPC 2.0 over AF_UNIX.

Written from hub/ADDONS.md section 2.2, standard library only. One reader
thread turns the socket into two streams: responses (resolved into whoever is
blocked in `request`) and everything else (handed to `on_message`, which the
add-on answers on a worker so a slow tool never stalls the reader).

A copy of the profile add-on's link (each bundle has its own PYTHONPATH). An
add-on that only answers its tools never calls `observe`; one that does is sent
what it asked for (tool calls, events, resource and registry changes) as
`hub.observation` notifications. `call` / `read` ask other providers via the hub.
"""
from __future__ import annotations

import json
import os
import select
import socket
import stat
import threading
import time
from typing import Any, Callable, Optional

PROTOCOL = 1


def _finite(o: Any) -> Any:
    """`o` with every infinite or NaN float as null - what JSON.stringify writes for them."""
    if isinstance(o, float):
        return o if o == o and o not in (float("inf"), float("-inf")) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


_TOKEN_FROM_HUB: Optional[str] = None


def _read_token_fd() -> Optional[str]:
    """The provider token of an add-on the hub started: one line in a pipe the hub handed it as an fd
    (AAH_PROVIDER_TOKEN_FD=<fd>:<hub pid>, hub/source/Extensions.cpp spawn) - never in the environment, which any
    program of the same user can read (sysctl kern.procargs2). Read once, when this module is imported, and the fd
    closed; the variable goes too, so nothing this add-on starts inherits it. Only when the hub that handed it is this
    process's parent: a program further down that inherited the variable leaves its own fd alone."""
    spec = os.environ.pop("AAH_PROVIDER_TOKEN_FD", "")
    fd_s, sep, parent_s = spec.partition(":")
    try:
        fd, parent = int(fd_s), int(parent_s)
    except ValueError:
        return None
    if not sep or fd < 3 or parent <= 1 or parent != os.getppid():
        return None
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            return None
    except OSError:
        return None
    data = b""
    try:
        if select.select([fd], [], [], 2.0)[0]:
            data = os.read(fd, 512)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    token = data.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
    return token or None


_TOKEN_FROM_HUB = _read_token_fd()


def provider_token() -> Optional[str]:
    """What this add-on presents in hub.hello: the hub's own (above), else AAH_PROVIDER_TOKEN - a developer's hand-run
    add-on, given the same token as the developer's test hub."""
    return _TOKEN_FROM_HUB or os.environ.get("AAH_PROVIDER_TOKEN") or None


class HubError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def socket_path() -> str:
    """The hub's socket. AF_UNIX sun_path is 104 bytes on macOS, so a long user
    name falls back to the user's own temporary folder (/var/folders/.../T, which
    only that user can write in); the same rule as hublink/JsonRpc.h hubSocketFile."""
    env = os.environ.get("AAH_HUB_SOCKET")
    if env:
        return env
    home = os.path.expanduser("~")
    p = f"{home}/Library/Application Support/Elephant Office/AiAudioHub/hub.sock"
    if len(p.encode()) < 100:
        return p
    try:
        tmp = os.confstr(65537)                    # _CS_DARWIN_USER_TEMP_DIR (Python has no name for it)
    except (OSError, ValueError):
        tmp = None
    if tmp and tmp.startswith("/"):
        return os.path.join(tmp, "aiaudiohub.sock")
    return f"/tmp/aiaudiohub-{os.getuid()}/hub.sock"


class HubLink:
    def __init__(self, on_message: Callable[[dict], None], on_close: Optional[Callable[[], None]] = None):
        self._on_message = on_message
        self._on_close = on_close
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._next_id = 1
        self._lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._reader: Optional[threading.Thread] = None
        self.connected = False

    # ---- lifecycle ---------------------------------------------------------
    def connect(self, path: Optional[str] = None, timeout: float = 5.0) -> None:
        fd = os.environ.pop("AAH_HUB_FD", None) if path is None else None
        if fd:
            # an add-on from Explore: the hub started it in a sandbox that opens no socket, and handed it this one,
            # connected (hub/source/Extensions.cpp spawn). Used once; a reconnect after it closes fails, and the hub
            # starts the add-on again with a new one.
            s = socket.socket(fileno=int(fd))
        else:
            path = path or socket_path()
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(path)
        s.settimeout(None)
        self._sock = s
        self._buf = b""
        self.connected = True
        self._reader = threading.Thread(target=self._read_loop, name="hub-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self.connected = False
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    # ---- sending -----------------------------------------------------------
    def _send(self, msg: dict) -> None:
        try:
            text = json.dumps(msg, separators=(",", ":"), allow_nan=False)
        except ValueError:      # -inf dB, NaN: not JSON - the hub dropped such a reply whole (a silent time-out)
            text = json.dumps(_finite(msg), separators=(",", ":"))
        line = (text + "\n").encode()
        with self._lock:
            s = self._sock
            if s is None:
                raise HubError(-32000, "not connected to the hub")
            s.sendall(line)

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def reply(self, mid: Any, result: Any) -> None:
        self._send({"jsonrpc": "2.0", "id": mid, "result": result})

    def fail(self, mid: Any, code: int, message: str, data: Optional[dict] = None) -> None:
        """`data`: what the failure says beyond its words - {code, fix, candidates} (the hub's ToolContract.h)."""
        err: dict = {"code": code, "message": message}
        if data:
            err["data"] = data
        self._send({"jsonrpc": "2.0", "id": mid, "error": err})

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> Any:
        """Send a request and block until its response. Raises HubError."""
        with self._lock:
            mid = self._next_id
            self._next_id += 1
            slot = {"event": threading.Event(), "response": None}
            self._pending[mid] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
            if not slot["event"].wait(timeout):
                raise HubError(-32001, f"the hub did not answer {method} within {timeout:.0f} s")
        finally:
            with self._lock:
                self._pending.pop(mid, None)
        resp = slot["response"]
        if resp is None:
            raise HubError(-32002, "connection closed while waiting for " + method)
        if "error" in resp:
            err = resp["error"] or {}
            raise HubError(int(err.get("code", -32000)), str(err.get("message", "error")))
        return resp.get("result")

    # ---- the hub's vocabulary, as methods ----------------------------------
    def hello(self, product: str, version: str) -> dict:
        params = {"protocol": PROTOCOL, "product": product, "version": version, "pid": os.getpid()}
        token = provider_token()   # the hub hands it to the add-ons it launches itself (through a pipe, not the environment)
        if token:
            params["token"] = token
        r = self.request("hub.hello", params)
        if int(r.get("protocol", 0)) != PROTOCOL:
            raise HubError(-32003, f"hub speaks protocol {r.get('protocol')}, this add-on speaks {PROTOCOL}")
        return r

    def register(self, instance: dict, manifest: dict) -> None:
        self.request("provider.register", {"instance": instance, "manifest": manifest})

    def unregister(self, instance_id: str) -> None:
        try:
            self.notify("provider.unregister", {"instance": instance_id})
        except HubError:
            pass

    def observe(self, kinds=("call", "event", "resource", "registry")) -> list:
        r = self.request("hub.observe", {"kinds": list(kinds)})
        return list(r.get("kinds", []))

    def call(self, tool: str, args: Optional[dict] = None, *, instance: Optional[str] = None,
             namespace: Optional[str] = None, timeout: float = 30.0) -> Any:
        """Ask another provider a READING tool through the hub."""
        p: dict = {"tool": tool, "args": args or {}}
        if instance:
            p["instance"] = instance
        if namespace:
            p["namespace"] = namespace
        return self.request("hub.call", p, timeout=timeout)

    def read(self, resource: str, *, instance: Optional[str] = None, namespace: Optional[str] = None) -> Any:
        p: dict = {"resource": resource}
        if instance:
            p["instance"] = instance
        if namespace:
            p["namespace"] = namespace
        return self.request("hub.read", p)

    def resource_changed(self, instance_id: str, resource: str) -> None:
        self.notify("provider.resourceChanged", {"instance": instance_id, "resource": resource})

    def emit(self, instance_id: str, event: str, data: Optional[dict] = None) -> None:
        self.notify("provider.event", {"instance": instance_id, "event": event, "data": data or {}})

    def update(self, instance_id: str, host: str, track: str) -> None:
        self.notify("provider.update", {"instance": instance_id, "host": host, "track": track})

    # ---- reading -----------------------------------------------------------
    def _read_loop(self) -> None:
        s = self._sock
        try:
            while s is not None and self.connected:
                try:
                    chunk = s.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                self._buf += chunk
                while b"\n" in self._buf:
                    line, self._buf = self._buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    self._dispatch(msg)
        finally:
            self.connected = False
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for slot in pending:
                slot["event"].set()
            if self._on_close:
                try:
                    self._on_close()
                except Exception:
                    pass

    def _dispatch(self, msg: dict) -> None:
        if "method" not in msg and ("result" in msg or "error" in msg):
            mid = msg.get("id")
            with self._lock:
                slot = self._pending.get(mid) if isinstance(mid, int) else None
            if slot is not None:
                slot["response"] = msg
                slot["event"].set()
            return
        try:
            self._on_message(msg)
        except Exception as e:  # a handler bug must not kill the reader
            mid = msg.get("id")
            if mid is not None:
                try:
                    self.fail(mid, -32603, f"{type(e).__name__}: {e}")
                except HubError:
                    pass


def now_ms() -> int:
    return int(time.time() * 1000)
