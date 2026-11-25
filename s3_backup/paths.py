"""Local path confinement: every local file a push reads or a pull writes lives under one backup directory.

The backup directory is ``S3_BACKUP_DIR`` when set, else ``<home>/plugin-data/s3/backups`` where ``<home>`` is the
active profile home. Relative local paths are taken relative to it. A path is accepted only when its fully
resolved form (every symlink followed) is the backup directory or inside it, so neither ``../`` nor a symlink can
reach the profile's secrets, plugins or config. The backup directory itself may not be reached through a symlink,
may not contain the profile home, and inside the profile home must sit under ``plugin-data``.
"""
from __future__ import annotations

import os
from pathlib import Path


class PathRefused(ValueError):
    """A local path resolves outside the backup directory."""


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # the active profile home when loaded in the agent
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))


def backup_dir() -> Path:
    """The configured backup directory (not created). Refused when any part of it below the profile home, or any part
    of a configured ``S3_BACKUP_DIR``, is a symlink: a planted link must not turn plugins or keys into the backup directory."""
    home = Path(os.path.realpath(_home()))
    configured = os.environ.get("S3_BACKUP_DIR")
    base = Path(os.path.expanduser(configured)) if configured else home / "plugin-data" / "s3" / "backups"
    if not base.is_absolute():
        raise PathRefused("S3_BACKUP_DIR must be an absolute path")
    base = Path(os.path.normpath(base))
    root = Path(os.path.realpath(base))
    if root != base:
        raise PathRefused(f"The backup directory may not be reached through a symlink: {base} resolves to {root}")
    if _within(home, root):
        raise PathRefused(f"The backup directory may not contain the profile home: {root}")
    if _within(root, home) and not _within(root, home / "plugin-data"):
        raise PathRefused(f"Inside the profile home the backup directory must sit under plugin-data: {root}")
    return root


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(path))) == str(root)
    except ValueError:
        return False


def confine(local: str, *, create_root: bool = False) -> Path:
    """Resolve ``local`` (relative to the backup directory unless absolute) and refuse it unless it stays inside."""
    if not isinstance(local, str) or not local.strip():
        raise PathRefused("A local path is required")
    root = backup_dir()
    if create_root and not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        root = backup_dir()  # re-check: nothing may have been swapped in while it was created
    expanded = os.path.expanduser(local)
    candidate = Path(expanded) if os.path.isabs(expanded) else root / expanded
    resolved = Path(os.path.realpath(candidate))
    if not _within(resolved, root):
        raise PathRefused(f"Local path is outside the backup directory {root}: {local}")
    return resolved


def is_inside(path: Path, root: Path) -> bool:
    """Whether ``path`` resolves (symlinks followed) to ``root`` or below it."""
    return _within(Path(os.path.realpath(path)), Path(os.path.realpath(root)))
