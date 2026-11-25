"""s3_backup — plugin package for S3-compatible backup/restore.

This package holds the plugin logic. The repository-root ``__init__.py``
is a thin entry shim that re-exports :func:`register` so Hermes can
load ``<plugin-dir>/__init__.py`` directly.
"""

from __future__ import annotations

from pathlib import Path

try:
    from . import approval, tools
    from .cli import register as register_cli
    from .schemas import (
        S3_BACKUP_LS_SCHEMA,
        S3_BACKUP_PULL_SCHEMA,
        S3_BACKUP_PUSH_SCHEMA,
        S3_BACKUP_RM_SCHEMA,
    )
except ImportError:  # top-level import when run as a script (tests)
    import approval
    import tools
    from cli import register as register_cli
    from schemas import (
        S3_BACKUP_LS_SCHEMA,
        S3_BACKUP_PULL_SCHEMA,
        S3_BACKUP_PUSH_SCHEMA,
        S3_BACKUP_RM_SCHEMA,
    )

_PLUGIN_DIR = Path(__file__).resolve().parent
_SKILL_NAME = "s3"

_SCHEMAS = {
    "s3-push": S3_BACKUP_PUSH_SCHEMA,
    "s3-pull": S3_BACKUP_PULL_SCHEMA,
    "s3-ls": S3_BACKUP_LS_SCHEMA,
    "s3-rm": S3_BACKUP_RM_SCHEMA,
}


def register(ctx) -> None:
    """Register the s3 tools, the approval hook for push/pull/rm, the CLI and the skill."""
    ctx.register_hook("pre_tool_call", approval.hook)
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="s3",
            schema=schema,
            handler=tools.HANDLERS[name],
            check_fn=tools.available,
            override=False,
            description=schema["description"],
            emoji="📦",
        )

    ctx.register_cli_command(
        name="s3",
        help="Push/pull/ls/rm files to any S3-compatible bucket",
        setup_fn=register_cli,
        handler_fn=None,
    )

    skill_path = _PLUGIN_DIR.parent / "skills" / _SKILL_NAME / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(_SKILL_NAME, skill_path)
