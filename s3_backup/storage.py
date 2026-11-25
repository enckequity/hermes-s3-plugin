"""Boto3-backed storage with overwrite and restore-path safety."""
from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

try:
    from . import paths
    from .config import Config, load_config
except ImportError:  # top-level import when run as a script (tests)
    import paths
    from config import Config, load_config

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _make_client(cfg: Config) -> Any:
    try:
        import boto3
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "boto3 is required for S3 backup operations; install the 'boto3' package"
        ) from exc
    cfg.require_storage()
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        region_name=cfg.region,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key,
    )


def _client_for_bucket(bucket: str, cfg: Config | None = None) -> tuple[Any, Config]:
    if not isinstance(bucket, str) or not bucket:
        raise ValueError("S3 backup bucket must be a non-empty string")
    cfg = cfg or load_config()
    if cfg.bucket != bucket:
        cfg = replace(cfg, bucket=bucket)
    return _make_client(cfg), cfg


def _safe_error(exc: Exception, cfg: Config | None = None) -> dict[str, str]:
    message = str(exc)
    if cfg:
        for secret in (cfg.access_key, cfg.secret_key):
            if secret:
                message = message.replace(secret, "[redacted]")
    return {"type": type(exc).__name__, "message": message or "S3 operation failed"}


def _list_objects(client: Any, bucket: str, prefix: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        response = client.list_objects_v2(**kwargs)
        for item in response.get("Contents", []) or []:
            if isinstance(item, dict) and isinstance(item.get("Key"), str):
                objects.append(item)
        if not response.get("IsTruncated"):
            return objects
        next_token = response.get("NextContinuationToken")
        if not next_token or next_token == token:
            raise RuntimeError("S3 listing was truncated without a continuation token")
        token = next_token


def _object_exists(client: Any, bucket: str, key: str) -> bool:
    return any(item.get("Key") == key for item in _list_objects(client, bucket, key))


def _local_files(local: str) -> tuple[Path, list[tuple[Path, str]], list[str]]:
    """Regular files to upload from ``local``, confined to the backup directory. Symlinks are never followed:
    one named directly must resolve inside the backup directory, and any found while walking is skipped."""
    root = paths.confine(local)
    if root.is_file():
        if os.stat(root).st_nlink > 1:
            raise ValueError(f"Refusing to upload a file with other hard links: {local}")
        return root, [(root, root.name)], []
    if not root.is_dir():
        raise FileNotFoundError(f"Local source does not exist or is not a file/directory: {local}")
    files: list[tuple[Path, str]] = []
    links: list[str] = []
    for path, dirnames, names in os.walk(root, followlinks=False):
        for name in sorted(dirnames):
            if os.path.islink(os.path.join(path, name)):
                links.append((Path(path) / name).relative_to(root).as_posix())
        for name in sorted(names):
            candidate = Path(path) / name
            mode = os.lstat(candidate).st_mode
            if stat.S_ISLNK(mode):
                links.append(candidate.relative_to(root).as_posix())
            elif stat.S_ISREG(mode) and os.lstat(candidate).st_nlink > 1:
                # A hard link can name a file outside the backup directory (the profile's .env); skip it like a symlink.
                links.append(candidate.relative_to(root).as_posix())
            elif stat.S_ISREG(mode) and paths.is_inside(candidate, root):
                files.append((candidate, candidate.relative_to(root).as_posix()))
    files.sort(key=lambda item: item[1])
    return root, files, sorted(links)


def _open_upload(path: Path):
    """Open a regular file for reading without following a symlink swapped in after the walk."""
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise ValueError(f"Refusing to upload a non-regular or hard-linked file: {path}")
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def _join_key(prefix: str, relative: str) -> str:
    prefix = prefix.rstrip("/")
    return f"{prefix}/{relative}" if prefix else relative


def push(local: str, remote: str, bucket: str, force: bool = False, cfg: Config | None = None) -> tuple[bool, dict[str, Any]]:
    cfg: Config | None = None
    try:
        if not isinstance(remote, str) or not remote:
            raise ValueError("S3 backup remote destination must be a non-empty string")
        root, files, links = _local_files(local)
        client, cfg = _client_for_bucket(bucket, cfg)
        is_file = root.is_file()
        report: dict[str, Any] = {"local": str(root), "files": 0, "bytes": 0, "skipped": 0, "failed": []}
        if links:
            report["skipped_links"] = links
        for path, relative in files:
            key = remote if is_file else _join_key(remote, relative)
            try:
                if not force and _object_exists(client, bucket, key):
                    report["skipped"] += 1
                    continue
                with _open_upload(path) as stream:
                    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key, "Body": stream}
                    if not force:
                        kwargs["IfNoneMatch"] = "*"
                    try:
                        client.put_object(**kwargs)
                    except Exception:
                        # Some S3-compatible stores reject the conditional, or a
                        # mock client does not expose it. Re-check existence and
                        # never replace a new object before the compatibility call.
                        if not force and _object_exists(client, bucket, key):
                            report["skipped"] += 1
                            continue
                        client.put_object(Bucket=bucket, Key=key, Body=stream)
                report["files"] += 1
                report["bytes"] += path.stat().st_size
            except Exception as exc:
                report["failed"].append({"key": key, "error": _safe_error(exc, cfg)})
        report["ok"] = not report["failed"]
        return report["ok"], report
    except Exception as exc:
        return False, {"error": _safe_error(exc, cfg)}


def _restore_path(target: str, key: str, relative: str) -> Path:
    if os.path.isabs(key) or os.path.isabs(relative):
        raise ValueError(f"Refusing unsafe absolute S3 object key: {key}")
    parts = relative.split("/")
    if any(part in ("..", "") for part in parts):
        raise ValueError(f"Refusing unsafe S3 object key: {key}")
    target_abs = os.path.abspath(Path(target).expanduser())
    target_real = os.path.realpath(target_abs)
    output = os.path.abspath(os.path.join(target_abs, *parts))
    output_real = os.path.realpath(output)
    try:
        contained = os.path.commonpath((target_real, output_real)) == target_real
        parent_contained = os.path.commonpath(
            (target_real, os.path.realpath(os.path.dirname(output)))
        ) == target_real
    except ValueError:
        contained = parent_contained = False
    if not contained or not parent_contained:
        raise ValueError(f"Refusing S3 object key outside restore directory: {key}")
    return Path(output)


def _read_body_to_file(body: Any, destination: Path) -> int:
    """Write into a new temporary file beside ``destination`` and rename it into place, so an existing symlink or hard
    link at the destination is replaced, never written through."""
    fd, temporary = tempfile.mkstemp(prefix=".s3-pull-", dir=destination.parent)
    written = 0
    try:
        with os.fdopen(fd, "wb") as stream:
            if isinstance(body, (bytes, bytearray)):
                stream.write(body)
                written = len(body)
            else:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
                    written += len(chunk)
        os.replace(temporary, destination)
        return written
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def pull(remote: str, local: str, bucket: str, cfg: Config | None = None) -> tuple[bool, dict[str, Any]]:
    cfg: Config | None = None
    try:
        if not isinstance(remote, str) or not remote:
            raise ValueError("S3 backup remote source must be a non-empty string")
        target = paths.confine(local, create_root=True)
        client, cfg = _client_for_bucket(bucket, cfg)
        listed = _list_objects(client, bucket, remote)
        exact = any(item.get("Key") == remote for item in listed)
        prefix = remote.rstrip("/") + "/"
        # Without an exact match only keys below ``remote/`` belong to it; ``backups`` must not pull ``backups-old/x``.
        selected = [item for item in listed if item.get("Key") == remote] if exact else [
            item for item in listed if item["Key"].startswith(prefix)]
        destinations: list[tuple[dict[str, Any], Path]] = []
        for item in selected:
            key = item["Key"]
            relative = os.path.basename(key) if exact else key[len(prefix) :]
            destinations.append((item, _restore_path(str(target), key, relative)))
        report: dict[str, Any] = {"local": str(target), "files": 0, "bytes": 0}
        for item, destination in destinations:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not paths.is_inside(destination.parent, target):
                raise ValueError(f"Refusing S3 object key outside restore directory: {item['Key']}")
            response = client.get_object(Bucket=bucket, Key=item["Key"])
            report["bytes"] += _read_body_to_file(response["Body"], destination)
            report["files"] += 1
        return True, report
    except Exception as exc:
        return False, {"error": _safe_error(exc, cfg)}


def ls(prefix: str, bucket: str, cfg: Config | None = None) -> tuple[bool, dict[str, Any]]:
    cfg: Config | None = None
    try:
        if not isinstance(prefix, str) or not prefix:
            raise ValueError("S3 backup prefix must be a non-empty string")
        client, cfg = _client_for_bucket(bucket, cfg)
        objects = []
        for item in _list_objects(client, bucket, prefix):
            metadata: dict[str, Any] = {"key": item["Key"]}
            if "Size" in item:
                metadata["size"] = item["Size"]
            if "LastModified" in item:
                value = item["LastModified"]
                metadata["last_modified"] = value.isoformat() if hasattr(value, "isoformat") else str(value)
            objects.append(metadata)
        return True, {"keys": [item["key"] for item in objects], "objects": objects}
    except Exception as exc:
        return False, {"error": _safe_error(exc, cfg)}


def rm(key: str, bucket: str, confirm: bool, recursive: bool = False, cfg: Config | None = None) -> tuple[bool, dict[str, Any]]:
    if not confirm:
        return False, {"error": {"type": "ConfirmationError", "message": "Deletion requires confirm=true"}}
    cfg: Config | None = None
    try:
        if not isinstance(key, str) or not key:
            raise ValueError("S3 backup key or prefix must be a non-empty string")
        client, cfg = _client_for_bucket(bucket, cfg)
        if not recursive:
            below = key.rstrip("/") + "/"
            matching = _list_objects(client, bucket, key)
            if any(item.get("Key") != key and item["Key"].startswith(below) for item in matching):
                return False, {"error": {"type": "SafetyError", "message": "Deleting a prefix requires recursive=true"}}
            client.delete_object(Bucket=bucket, Key=key)
            return True, {"files": 1, "deleted": 1}
        below = key.rstrip("/") + "/"
        # Only the key itself and keys below ``key/``; deleting ``backups`` must not take ``backups-old/*``.
        objects = [item for item in _list_objects(client, bucket, key) if item["Key"] == key or item["Key"].startswith(below)]
        deleted = 0
        for item in objects:
            client.delete_object(Bucket=bucket, Key=item["Key"])
            deleted += 1
        return True, {"files": deleted, "deleted": deleted}
    except Exception as exc:
        return False, {"error": _safe_error(exc, cfg)}
