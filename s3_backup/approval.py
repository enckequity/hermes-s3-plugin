"""Native per-call approval for the s3 writes (push, pull, rm).

A ``pre_tool_call`` hook returns ``{"action": "approve", "message", "rule_key"}``. The rule_key is the tool name plus a
digest of every argument the call can carry, what a named rule resolves to right now (local path resolved inside the
backup directory, remote key or prefix), and the configured endpoint and bucket. An "always" answer therefore covers only
that exact call against that store, and stops covering a rule call once its rules-file entry changes. A rule with a
``{ts}`` placeholder resolves differently on every call, so it asks every time.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

try:
    from . import config, paths, rules
except ImportError:  # top-level import when run as a script (tests)
    import config
    import paths
    import rules

# Every name a write is reachable under (the registered names and the legacy aliases in tools.HANDLERS).
GATED = {
    "s3-push": "push", "s3_backup_push": "push",
    "s3-pull": "pull", "s3_backup_pull": "pull",
    "s3-rm": "rm", "s3_backup_rm": "rm",
}
_FIELDS = ("src", "dst", "local", "remote", "rule", "force", "key", "prefix", "recursive", "confirm")
_LIMIT = 450


def _bucket() -> str:
    return os.environ.get("S3_BACKUP_BUCKET") or ""


def _resolved(tool_name: str, args: dict) -> dict:
    """What a rule supplies to this call, with its local path resolved the way the handler will resolve it."""
    op = GATED.get(tool_name)
    if not op or not args.get("rule"):
        return {}
    try:
        values = rules.resolve(args["rule"], op, config.load_config().rules_path)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    local = values.get("local")
    try:
        local = str(paths.confine(local)) if local else None
    except Exception:
        local = f"refused:{local}"
    return {"local": local, "remote": values.get("remote")}


def rule_key(tool_name: str, args: dict) -> str:
    payload = {"tool": tool_name, "endpoint": os.environ.get("S3_BACKUP_ENDPOINT") or "", "bucket": _bucket(),
               "resolved": _resolved(tool_name, args), **{k: args.get(k) for k in _FIELDS}}
    # Every other argument too, so nothing the handler might read later escapes the binding.
    payload["extra"] = {k: args[k] for k in sorted(args) if k not in _FIELDS}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"{tool_name}:{digest[:16]}"


def _first(args: dict, *names: str) -> Any:
    for name in names:
        if args.get(name) is not None:
            return args[name]
    return None


def _rule_values(args: dict, op: str) -> dict:
    name = args.get("rule")
    if not name:
        return {"local": None, "remote": None}
    try:
        return rules.resolve(name, op, config.load_config().rules_path)
    except Exception:
        return {"local": None, "remote": None}


def _local_label(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "(none)"
    try:
        return str(paths.confine(value))
    except Exception:
        return f"{value} (outside the backup directory; will be refused)"


def _text(value: Any) -> str:
    return value if isinstance(value, str) and value else "(none)"


def _message(op: str, args: dict) -> str:
    values = _rule_values(args, op)
    bucket = _bucket() or "(no bucket configured)"
    rule = f" (rule {args['rule']})" if args.get("rule") else ""
    if op == "push":
        local = _first(args, "src", "local") or values["local"]
        remote = _first(args, "dst", "remote") or values["remote"]
        mode = "overwriting existing objects" if args.get("force") is True else "skipping existing objects"
        return f"Upload {_local_label(local)} to bucket {bucket} at {_text(remote)}{rule}, {mode}."
    if op == "pull":
        remote = _first(args, "src", "remote") or values["remote"]
        local = _first(args, "dst", "local") or values["local"]
        return f"Download {_text(remote)} from bucket {bucket} into {_local_label(local)}{rule}, replacing files of the same name."
    target = args.get("key") or args.get("prefix") or values["remote"]
    what = "every object under prefix" if not args.get("key") else "object"
    return f"Delete {what} {_text(target)} from bucket {bucket}{rule}. This cannot be undone."


def hook(tool_name: str = "", args: dict | None = None, **_: Any):
    op = GATED.get(tool_name)
    if op is None:
        return None
    args = args if isinstance(args, dict) else {}
    try:
        message = _message(op, args)
    except Exception:
        message = f"Approve {tool_name}"
    if len(message) > _LIMIT:
        message = message[: _LIMIT - 3] + "..."
    return {"action": "approve", "message": message, "rule_key": rule_key(tool_name, args)}
