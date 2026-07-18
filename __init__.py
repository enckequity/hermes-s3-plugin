"""Hermes plugin entry point for s3.

Thin shim: Hermes loads ``<plugin-dir>/__init__.py``; the real
logic lives in the :mod:`s3_backup` package, so we re-export
:func:`register` (and :func:`register_cli`, used by the CLI
setup path) from here.
"""

from s3_backup import register
from s3_backup.cli import register as register_cli

__all__ = ["register", "register_cli"]
