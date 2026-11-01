"""The small toolkit this add-on's tools are written with: the tool table, argument checks, errors, settings, the data
folder and the manifest the hub reads. tools.py is where the add-on's own work goes; this file rarely changes.

Standard library only, Python 3.9 or newer.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Callable, Dict, List, Optional

Handler = Callable[[dict], Any]

# filled by tools.py through tool() and setting()
TOOLS: List[dict] = []
SETTINGS: List[dict] = []
# the user's settings: every declared default first, then what the hub sends (provider.settings)
VALUES: Dict[str, Any] = {}

_BUNDLE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ToolError(ValueError):
    """A refusal the assistant reads. The message IS the interface: say what was wrong and what to send instead.
    `code` is a short word for the kind of failure, `field` the argument at fault, `candidates` the values that would
    work (one candidate lets the hub offer the fixed call)."""

    def __init__(self, message: str, code: str = "invalid_arguments", field: str = "", candidates=None):
        super().__init__(message)
        self.code = code
        self.field = field
        self.candidates = list(candidates or [])


def bundle_manifest() -> dict:
    """addon/manifest.json, the one place the add-on's id, namespace and version are written."""
    with open(os.path.join(_BUNDLE, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def schema(props: Optional[dict] = None, required=()) -> dict:
    return {"type": "object", "properties": props or {}, "required": list(required), "additionalProperties": False}


def prop(type_: str, description: str, **extra) -> dict:
    d = {"type": type_, "description": description}
    d.update(extra)
    return d


def tool(name: str, description: str, *, props: Optional[dict] = None, required=(), writes: bool = False,
         timeout_ms: int = 0, module: str = ""):
    """Declares a tool. `writes`: true for anything that changes something (a file, a setting, the world outside); the
    hub then holds it behind the user's AI LOCK and asks the user before it runs."""

    def deco(fn: Handler) -> Handler:
        entry = {"name": name, "description": description, "writes": bool(writes),
                 "inputSchema": schema(props, required), "handler": fn}
        if module:
            entry["module"] = module
        if timeout_ms:
            entry["timeoutMs"] = int(timeout_ms)
        TOOLS.append(entry)
        return fn

    return deco


def setting(key: str, type_: str, label: str, default: Any, hint: str = "", **extra) -> None:
    """Declares a setting the hub's window draws for the user (text, toggle, select, number, path, table)."""
    d = {"key": key, "type": type_, "label": label, "default": default}
    if hint:
        d["hint"] = hint
    d.update(extra)
    SETTINGS.append(d)
    VALUES.setdefault(key, default)


def apply_settings(values: dict) -> None:
    """What the hub sent: one flat object, a module's keys prefixed with its id ("tempo.sampleRate")."""
    known = {s["key"] for s in SETTINGS}
    for key, value in (values or {}).items():
        key = str(key).split(".", 1)[-1]
        if key in known:
            VALUES[key] = value


def data_dir() -> str:
    """The one folder this add-on may write in. The hub sets AAH_ADDON_DATA; run by hand, a temporary folder."""
    d = os.environ.get("AAH_ADDON_DATA") or os.path.join(tempfile.gettempdir(), bundle_manifest().get("namespace", "addon"))
    os.makedirs(d, exist_ok=True)
    return d


def manifest(namespace: str, product: str, version: str) -> dict:
    """What provider.register sends: the tools (without their handlers) and the settings."""
    return {"namespace": namespace, "product": product, "version": version,
            "tools": [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS],
            "resources": [], "events": [], "settings": SETTINGS}


_TYPES = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,), "array": (list,),
          "object": (dict,)}


def _check(t: dict, args: dict) -> dict:
    """The arguments against the tool's own schema, so a wrong call is refused with words that fix it."""
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    sch = t["inputSchema"]
    props = sch.get("properties", {})
    for key in args:
        if key not in props:
            raise ToolError("%s takes no `%s`" % (t["name"], key), "unknown_argument", key, sorted(props))
    for key in sch.get("required", []):
        if key not in args:
            raise ToolError("%s needs `%s`: %s" % (t["name"], key, props.get(key, {}).get("description", "")),
                            "missing_argument", key)
    for key, value in args.items():
        p = props[key]
        want = _TYPES.get(p.get("type", ""))
        if want and (not isinstance(value, want) or (p.get("type") != "boolean" and isinstance(value, bool))):
            raise ToolError("`%s` must be a %s" % (key, p["type"]), "invalid_arguments", key)
        if "enum" in p and value not in p["enum"]:
            raise ToolError("`%s` must be one of %s" % (key, ", ".join(map(str, p["enum"]))), "invalid_arguments", key,
                            p["enum"])
        for bound, ok in (("minimum", lambda v, b: v >= b), ("maximum", lambda v, b: v <= b)):
            if bound in p and isinstance(value, (int, float)) and not ok(value, p[bound]):
                raise ToolError("`%s` must be %s %s" % (key, "at least" if bound == "minimum" else "at most", p[bound]),
                                "invalid_arguments", key)
    return args


def call(name: str, args: Optional[dict] = None) -> Any:
    t = next((t for t in TOOLS if t["name"] == name), None)
    if t is None:
        raise ToolError("there is no tool %s" % name, "unknown_tool", "", sorted(x["name"] for x in TOOLS))
    return t["handler"](_check(t, args if args is not None else {}))


def error_data(e: BaseException, namespace: str, name: str, args: Any) -> Optional[dict]:
    """What a refusal carries beyond its words: {code, candidates, fix} (one candidate = the fixed call)."""
    code = str(getattr(e, "code", "") or "")
    if not code:
        return None
    data: dict = {"code": code}
    cands = list(getattr(e, "candidates", []) or [])
    if cands:
        data["candidates"] = cands[:5]
    field = str(getattr(e, "field", "") or "")
    if field and len(cands) == 1 and isinstance(args, dict):
        data["fix"] = {"tool": "%s__%s" % (namespace, name), "arguments": {**args, field: cands[0]}}
    return data
