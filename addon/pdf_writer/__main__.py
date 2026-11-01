"""The process: connect to AiAudioHub, register the add-on, answer its tools.

    python3 -m tempo                 run by hand, with AiAudioHub running (it connects to the hub's socket)

The hub starts an add-on from Explore (or from a developer folder) itself, inside a sandbox, and hands it a connected
socket as file descriptor 3 (AAH_HUB_FD). link.py takes care of both. Nothing here needs to change for a new add-on:
the tools live in tools.py, the name and version in ../manifest.json.
"""
from __future__ import annotations

import os
import queue
import signal
import sys
import threading
import time
import uuid

from . import tools  # noqa: F401 - declares the tools and settings
from .link import HubError, HubLink
from .toolkit import apply_settings, bundle_manifest, call, error_data, manifest

M = bundle_manifest()
NAMESPACE, PRODUCT, VERSION = M["namespace"], M["name"], M["version"]


class Host:
    def __init__(self) -> None:
        self.link = HubLink(self.on_message)
        # an instance id starts with the add-on's namespace (the hub refuses one that does not)
        self.instance_id = "%s-%s" % (NAMESPACE, uuid.uuid4().hex[:8])
        self.work: "queue.Queue[tuple]" = queue.Queue()
        self.stop = threading.Event()

    # tools run on a worker: the reader thread keeps answering hub.ping while a tool works
    def worker(self) -> None:
        while not self.stop.is_set():
            try:
                mid, p = self.work.get(timeout=0.2)
            except queue.Empty:
                continue
            name, args = str(p.get("tool") or ""), p.get("args") or {}
            try:
                result = call(name, args)
                self.link.reply(mid, result if result is not None else {"ok": True})
            except (ValueError, TypeError, OSError) as e:
                self._fail(mid, -32602, str(e), error_data(e, NAMESPACE, name, args))
            except Exception as e:  # noqa: BLE001 - a bug is an error answer, never a dead add-on
                self._fail(mid, -32603, "%s: %s" % (type(e).__name__, e))

    def _fail(self, mid, code, message, data=None) -> None:
        try:
            self.link.fail(mid, code, message, data)
        except HubError:
            pass

    def on_message(self, msg: dict) -> None:
        method, mid, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "provider.call":
            self.work.put((mid, p))
        elif method == "provider.settings":
            apply_settings(p.get("values") or {})
            if mid is not None:
                self.link.reply(mid, {})
        elif method == "hub.ping":
            if mid is not None:
                self.link.reply(mid, {})
        elif method == "provider.read":
            self._fail(mid, -32601, "%s has no resources" % NAMESPACE)
        elif mid is not None and method:
            self._fail(mid, -32601, "unknown method %s" % method)

    def connect_once(self) -> None:
        self.link = HubLink(self.on_message)
        self.link.connect()
        self.link.hello(PRODUCT, VERSION)
        instance = {"id": self.instance_id, "namespace": NAMESPACE, "product": PRODUCT, "version": VERSION,
                    "host": "python %d.%d" % sys.version_info[:2], "track": PRODUCT}
        self.link.register(instance, manifest(NAMESPACE, PRODUCT, VERSION))
        print("registered %s as %s" % (self.instance_id, NAMESPACE), flush=True)

    def run(self) -> int:
        handed = bool(os.environ.get("AAH_HUB_FD"))     # started by the hub: one connection, never a reconnect
        threading.Thread(target=self.worker, name="worker", daemon=True).start()
        backoff = 1.0
        while not self.stop.is_set():
            try:
                self.connect_once()
                backoff = 1.0
                while self.link.connected and not self.stop.is_set():
                    time.sleep(0.25)
            except (OSError, HubError) as e:
                print("hub unreachable (%s)" % e, flush=True)
            if handed or self.stop.is_set():
                break                                    # the hub starts it again when it wants it
            print("retrying in %.0f s" % backoff, flush=True)
            self.stop.wait(backoff)
            backoff = min(backoff * 2.0, 30.0)
        if self.link.connected:
            self.link.unregister(self.instance_id)
            self.link.close()
        return 0

    def shutdown(self, *_) -> None:
        self.stop.set()


def main() -> int:
    host = Host()
    signal.signal(signal.SIGINT, host.shutdown)
    signal.signal(signal.SIGTERM, host.shutdown)
    return host.run()


if __name__ == "__main__":
    sys.exit(main())
