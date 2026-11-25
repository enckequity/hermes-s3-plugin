"""Closed tool schemas (name, description, parameters) for the four s3 tools."""
from __future__ import annotations

TOOLSET = "s3"
_PATH = {"type": "string", "minLength": 1}
_RULE = {"type": "string", "minLength": 1, "description": "Named rule from the rules file that supplies omitted paths."}
_LOCAL = {"type": "string", "minLength": 1,
          "description": "Local file or directory inside the backup directory; a relative path is taken from it."}
_APPROVAL = " Asks the user to approve each call before it runs."
_CONFINED = (" Local paths must stay inside the backup directory (S3_BACKUP_DIR, default plugin-data/s3/backups in the profile);"
             " anything resolving outside it, symlinks included, is refused.")


def _schema(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "additionalProperties": False},
    }


PUSH_SCHEMA = _schema(
    "s3-push",
    "Upload a file or directory from the backup directory to the configured S3-compatible bucket. Existing objects are"
    " skipped unless force is true; symlinks and hard-linked files inside a directory are skipped." + _CONFINED + _APPROVAL,
    {
        "src": _LOCAL, "local": _LOCAL,
        "dst": {**_PATH, "description": "Remote key (for a file) or prefix (for a directory)."},
        "remote": {**_PATH, "description": "Same as dst."},
        "rule": _RULE, "force": {"type": "boolean", "default": False, "description": "Overwrite existing objects."},
    },
)
PULL_SCHEMA = _schema(
    "s3-pull",
    "Download an object or every object under a prefix from the configured bucket into a directory inside the backup"
    " directory. Object keys that would escape it are refused." + _CONFINED + _APPROVAL,
    {
        "src": {**_PATH, "description": "Remote key or prefix to download."},
        "remote": {**_PATH, "description": "Same as src."},
        "dst": _LOCAL, "local": _LOCAL, "rule": _RULE,
    },
)
LS_SCHEMA = _schema(
    "s3-ls",
    "List objects (key, size, last modified) under a prefix in the configured bucket. Read-only.",
    {"prefix": {**_PATH, "description": "Remote prefix to list."}, "rule": _RULE},
)
RM_SCHEMA = _schema(
    "s3-rm",
    "Delete one object, or every object under a prefix with recursive=true, from the configured bucket. Needs"
    " confirm=true." + _APPROVAL,
    {
        "key": {**_PATH, "description": "Exact object key to delete."},
        "prefix": {**_PATH, "description": "Prefix whose objects are deleted (needs recursive=true)."},
        "rule": _RULE,
        "recursive": {"type": "boolean", "default": False},
        "confirm": {"type": "boolean", "default": False},
    },
)

S3_BACKUP_PUSH_SCHEMA = PUSH_SCHEMA
S3_BACKUP_PULL_SCHEMA = PULL_SCHEMA
S3_BACKUP_LS_SCHEMA = LS_SCHEMA
S3_BACKUP_RM_SCHEMA = RM_SCHEMA
