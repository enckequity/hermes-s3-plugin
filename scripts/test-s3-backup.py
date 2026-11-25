#!/usr/bin/env python3
"""Hermetic FakeS3 and FakeCtx tests for the standalone plugin."""
from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s3_backup import approval, paths, rules, schemas, storage  # noqa: E402


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deletes: list[str] = []
        self.gets = 0

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None):
        if IfNoneMatch == "*" and Key in self.objects:
            raise RuntimeError("precondition failed")
        self.objects[Key] = Body.read() if hasattr(Body, "read") else bytes(Body)

    def get_object(self, *, Bucket, Key):
        self.gets += 1
        return {"Body": io.BytesIO(self.objects[Key])}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start : start + 2]
        end = start + len(page)
        result = {"Contents": [{"Key": k, "Size": len(self.objects[k])} for k in page], "IsTruncated": end < len(keys)}
        if end < len(keys):
            result["NextContinuationToken"] = str(end)
        return result

    def delete_object(self, *, Bucket, Key):
        self.deletes.append(Key)
        self.objects.pop(Key, None)


class FakeCtx:
    def __init__(self):
        self.tools, self.cli, self.skills = [], [], []

        self.hooks = []

    def register_tool(self, **kwargs): self.tools.append(kwargs)
    def register_hook(self, name, fn): self.hooks.append((name, fn))
    def register_cli_command(self, *args, **kwargs): self.cli.append((args, kwargs))
    def register_skill(self, *args): self.skills.append(args)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def storage_tests(fake):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(os.path.realpath(tmp))
        os.environ["S3_BACKUP_DIR"] = str(root)
        file = root / "one.txt"
        file.write_bytes(b"one")
        ok, report = storage.push(str(file), "one.txt", "bucket", False)
        require(ok and report["files"] == 1 and report["bytes"] == 3, "file push")
        tree = root / "tree"
        (tree / "nested").mkdir(parents=True)
        (tree / "a").write_bytes(b"alpha")
        (tree / "nested" / "b").write_bytes(b"beta")
        ok, report = storage.push(str(tree), "backup", "bucket", False)
        require(ok and report["files"] == 2 and report["bytes"] == 9, "directory push")
        require(set(fake.objects) == {"one.txt", "backup/a", "backup/nested/b"}, "POSIX keys")
        file.write_bytes(b"changed")
        ok, report = storage.push(str(file), "one.txt", "bucket", False)
        require(ok and report["skipped"] == 1 and fake.objects["one.txt"] == b"one", "skip overwrite")
        ok, report = storage.push(str(file), "one.txt", "bucket", True)
        require(ok and report["files"] == 1 and fake.objects["one.txt"] == b"changed", "force overwrite")

        destination = root / "restore"
        ok, report = storage.pull("backup", str(destination), "bucket")
        require(ok and report == {"local": str(destination), "files": 2, "bytes": 9}, "round-trip report")
        require((destination / "nested" / "b").read_bytes() == b"beta", "round-trip content")
        gets = fake.gets
        ok, listing = storage.ls("backup", "bucket")
        require(ok and listing["keys"] == ["backup/a", "backup/nested/b"] and fake.gets == gets, "list metadata")

        fake.objects["evil/../escape"] = b"bad"
        fake.objects["evil/link/pwn"] = b"bad"
        outside = root / "outside"
        outside.mkdir()
        (destination / "link").symlink_to(outside, target_is_directory=True)
        ok, payload = storage.pull("evil", str(destination), "bucket")
        require(not ok and "unsafe" in payload["error"]["message"].lower(), "traversal guard")
        require(not (outside / "pwn").exists() and not (root / "escape").exists(), "escape prevention")

        before = len(fake.deletes)
        ok, _ = storage.rm("one.txt", "bucket", False)
        require(not ok and len(fake.deletes) == before, "confirmation required")
        ok, report = storage.rm("one.txt", "bucket", True)
        require(ok and report["deleted"] == 1, "exact deletion")
        fake.objects.update({"old/a": b"a", "old/b": b"bb"})
        ok, _ = storage.rm("old", "bucket", True, False)
        require(not ok, "recursive deletion guard")
        ok, report = storage.rm("old", "bucket", True, True)
        require(ok and report["deleted"] == 2, "recursive deletion")


def confinement_tests(fake):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(os.path.realpath(tmp))
        home = base / "home"
        backups = base / "backups"
        (home / "plugins" / "victim").mkdir(parents=True)
        backups.mkdir()
        (home / ".env").write_text("SECRET=x")
        os.environ.update({"HERMES_HOME": str(home), "S3_BACKUP_DIR": str(backups)})
        fake.objects.clear()

        for local in (str(home / ".env"), "../home/.env", str(home), "~/../../../outside-anything"):
            ok, payload = storage.push(local, "leak", "bucket", True)
            require(not ok and "outside the backup directory" in payload["error"]["message"], f"push refuses {local}")
        require(not fake.objects, "nothing uploaded from outside")

        (backups / "env-link").symlink_to(home / ".env")
        ok, payload = storage.push("env-link", "leak", "bucket", True)
        require(not ok and "outside the backup directory" in payload["error"]["message"], "push refuses a symlinked file")
        tree = backups / "tree"
        tree.mkdir()
        (tree / "keep.txt").write_bytes(b"keep")
        (tree / "env").symlink_to(home / ".env")
        (tree / "homedir").symlink_to(home, target_is_directory=True)
        ok, report = storage.push("tree", "tree", "bucket", False)
        require(ok and report["files"] == 1 and set(fake.objects) == {"tree/keep.txt"}, f"symlinks skipped in a walk: {report}")
        require(report["skipped_links"] == ["env", "homedir"], "skipped symlinks reported")
        require(report["local"] == str(tree), "relative push resolves under the backup directory")

        fake.objects["plug/__init__.py"] = b"import os"
        for local in (str(home / "plugins" / "victim"), "../home/plugins/victim", str(base)):
            ok, payload = storage.pull("plug", local, "bucket")
            require(not ok and "outside the backup directory" in payload["error"]["message"], f"pull refuses {local}")
        require(not (home / "plugins" / "victim" / "__init__.py").exists(), "nothing written into plugins")
        (backups / "plugins-link").symlink_to(home / "plugins", target_is_directory=True)
        ok, payload = storage.pull("plug", "plugins-link/victim", "bucket")
        require(not ok and "outside the backup directory" in payload["error"]["message"], "pull refuses a symlinked directory")
        require(not (home / "plugins" / "victim" / "__init__.py").exists(), "nothing written through a symlink")
        restore = backups / "restore"
        restore.mkdir()
        (restore / "__init__.py").symlink_to(home / ".env")
        ok, payload = storage.pull("plug", "restore", "bucket")
        require(not ok and (home / ".env").read_text() == "SECRET=x", "pull never writes through a file symlink")
        ok, report = storage.pull("plug", "fresh", "bucket")
        require(ok and (backups / "fresh" / "__init__.py").read_bytes() == b"import os", "relative pull lands in the backup directory")

        os.environ["S3_BACKUP_DIR"] = str(base)
        ok, payload = storage.push("backups/fresh", "x", "bucket", True)
        require(not ok and "may not contain the profile home" in payload["error"]["message"], "backup dir may not contain home")
        os.environ["S3_BACKUP_DIR"] = "relative/dir"
        ok, payload = storage.push("x", "x", "bucket", True)
        require(not ok and "absolute" in payload["error"]["message"], "backup dir must be absolute")
        os.environ.pop("S3_BACKUP_DIR")
        require(paths.backup_dir() == home / "plugin-data" / "s3" / "backups", "default backup directory")


def review_fix_tests(fake):
    """Backup directory symlinks, hard links, sibling prefixes, the CLI rule path and strict booleans."""
    from s3_backup import cli, tools
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(os.path.realpath(tmp))
        home = base / "home"
        (home / "plugins" / "victim").mkdir(parents=True)
        (home / ".env").write_text("SECRET=x")
        os.environ["HERMES_HOME"] = str(home)
        fake.objects.clear()

        # The default backup directory planted as a symlink to plugins is refused, for push and pull.
        os.environ.pop("S3_BACKUP_DIR", None)
        (home / "plugin-data" / "s3").mkdir(parents=True)
        (home / "plugin-data" / "s3" / "backups").symlink_to(home / "plugins", target_is_directory=True)
        fake.objects["p/__init__.py"] = b"evil"
        ok, payload = storage.pull("p", "victim", "bucket")
        require(not ok and "symlink" in payload["error"]["message"], f"symlinked backup dir refused for pull: {payload}")
        ok, payload = storage.push("victim", "x", "bucket", True)
        require(not ok and "symlink" in payload["error"]["message"], "symlinked backup dir refused for push")
        require(not (home / "plugins" / "victim" / "__init__.py").exists(), "nothing written through the planted link")
        (home / "plugin-data" / "s3" / "backups").unlink()
        os.environ["S3_BACKUP_DIR"] = str(base / "link-dir")
        (base / "link-dir").symlink_to(home, target_is_directory=True)
        ok, payload = storage.push(".env", "x", "bucket", True)
        require(not ok and "symlink" in payload["error"]["message"], "configured symlinked backup dir refused")
        os.environ["S3_BACKUP_DIR"] = str(home / "plugins")
        ok, payload = storage.pull("p", "victim", "bucket")
        require(not ok and "plugin-data/s3" in payload["error"]["message"], "backup dir inside home must be under plugin-data/s3")
        os.environ["S3_BACKUP_DIR"] = str(home / "plugin-data")
        ok, payload = storage.pull("p", "victim", "bucket")
        require(not ok and "plugin-data/s3" in payload["error"]["message"], "other plugins' data refused")

        # Pull creates the default directory on first use.
        os.environ.pop("S3_BACKUP_DIR")
        ok, report = storage.pull("p", "restored", "bucket")
        backups = home / "plugin-data" / "s3" / "backups"
        require(ok and (backups / "restored" / "__init__.py").read_bytes() == b"evil", f"default dir created: {report}")

        # Hard links: never uploaded, never written through.
        os.link(home / ".env", backups / "env-hard")
        ok, payload = storage.push("env-hard", "leak", "bucket", True)
        require(not ok and "hard link" in payload["error"]["message"], "hard-linked file push refused")
        tree = backups / "tree"
        tree.mkdir()
        (tree / "ok").write_bytes(b"ok")
        os.link(home / ".env", tree / "env-hard")
        ok, report = storage.push("tree", "tree", "bucket", True)
        require(ok and "tree/env-hard" not in fake.objects and report["skipped_links"] == ["env-hard"], f"hard link skipped: {report}")
        (backups / "restore2").mkdir()
        target = home / "plugins" / "victim" / "mod.py"
        target.write_text("original")
        os.link(target, backups / "restore2" / "mod.py")
        fake.objects["hl/mod.py"] = b"evil"
        ok, report = storage.pull("hl", "restore2", "bucket")
        require(ok and target.read_text() == "original" and (backups / "restore2" / "mod.py").read_bytes() == b"evil",
                "pull replaces a hard link instead of writing through it")
        require(not [n for n in os.listdir(backups / "restore2") if n.startswith(".s3-pull-")], "no temporary files left")

        # Sibling prefixes are not part of a prefix.
        fake.objects.clear()
        fake.objects.update({"bk/a": b"a", "bk-old/x": b"x", "bk2/y": b"y"})
        ok, report = storage.pull("bk", "sib", "bucket")
        require(ok and report["files"] == 1 and sorted(os.listdir(backups / "sib")) == ["a"], f"pull ignores siblings: {report}")
        ok, report = storage.rm("bk", "bucket", True, True)
        require(ok and report["deleted"] == 1 and set(fake.objects) == {"bk-old/x", "bk2/y"}, "rm ignores siblings")
        fake.objects["bk"] = b"exact"
        ok, report = storage.rm("bk", "bucket", True, False)
        require(ok and "bk" not in fake.objects, "exact rm with a sibling prefix present")

        # Folder markers are skipped; a key plus recursive is shown as a prefix delete.
        fake.objects.update({"mk/": b"", "mk/sub/": b"", "mk/f": b"f"})
        ok, report = storage.pull("mk", "markers", "bucket")
        require(ok and report["files"] == 1 and os.listdir(backups / "markers") == ["f"], f"folder markers skipped: {report}")
        message = approval.hook("s3-rm", {"key": "mk", "recursive": True, "confirm": True})["message"]
        require("every object under prefix mk" in message, message)

        # Tilde paths resolve against the user's home, outside the backup directory.
        ok, payload = storage.push("~/.env", "x", "bucket", True)
        require(not ok and "outside the backup directory" in payload["error"]["message"], "tilde path refused")

        # The CLI rule path loads as a package, and --src/--dst follow the command's direction.
        rules_file = base / "rules.yaml"
        rules_file.write_text("nightly:\n  prefix: nightly/{date}\n  source: tree\n")
        os.environ["S3_BACKUP_RULES"] = str(rules_file)
        fake.objects.clear()
        require(cli.main(["push", "--rule", "nightly"]) == 0 and any(k.startswith("nightly/") for k in fake.objects), "CLI --rule push")
        require(cli.main(["push", "--src", "tree", "--dst", "flags"]) == 0 and "flags/ok" in fake.objects, "CLI push --src/--dst")
        require(cli.main(["pull", "--src", "flags", "--dst", "flagged"]) == 0 and (backups / "flagged" / "ok").exists(), "CLI pull --src/--dst")
        require(cli.main(["push", str(home / ".env"), "leak"]) == 1 and "leak" not in fake.objects, "CLI push confined")

        # Approval binds what a rule resolves to: editing the rules file changes the key.
        os.environ["S3_BACKUP_BUCKET"] = "bucket"
        before = approval.hook("s3-rm", {"rule": "nightly", "recursive": True, "confirm": True})
        rules_file.write_text("nightly:\n  prefix: everything\n  source: tree\n")
        after = approval.hook("s3-rm", {"rule": "nightly", "recursive": True, "confirm": True})
        require(before["rule_key"] != after["rule_key"] and "everything" in after["message"], "rule edit changes the approval")
        os.environ["S3_BACKUP_ENDPOINT"] = "https://other.example"
        require(approval.hook("s3-rm", {"rule": "nightly", "recursive": True, "confirm": True})["rule_key"] != after["rule_key"],
                "rule_key binds the endpoint")
        os.environ["S3_BACKUP_ENDPOINT"] = "https://invalid"
        os.environ.pop("S3_BACKUP_RULES")

        # Truthy non-booleans never overwrite or delete.
        fake.objects.clear()
        fake.objects["keep/ok"] = b"old"
        result = json.loads(tools.handle_push({"src": "tree", "dst": "keep", "force": "false"}))
        require(result["ok"] and fake.objects["keep/ok"] == b"old", f"force must be true to overwrite: {result}")
        result = json.loads(tools.handle_rm({"key": "keep/ok", "confirm": "yes"}))
        require(not result["ok"] and "keep/ok" in fake.objects, "confirm must be true")


def approval_tests():
    os.environ["S3_BACKUP_DIR"] = tempfile.mkdtemp()
    for name in ("s3-push", "s3-pull", "s3-rm", "s3_backup_push", "s3_backup_pull", "s3_backup_rm"):
        directive = approval.hook(name, {"src": "a", "dst": "b", "key": "k", "confirm": True})
        require(directive["action"] == "approve" and directive["message"], f"{name} asks")
        tool, digest = directive["rule_key"].split(":", 1)
        require(tool == name and len(digest) == 16 and int(digest, 16) >= 0, f"{name} rule_key shape")
    for name in ("s3-ls", "s3_backup_ls", "terminal", "freshdesk_reply_to_ticket"):
        require(approval.hook(name, {"prefix": "a"}) is None, f"{name} not gated")
    base = {"src": "a.tar", "dst": "backups/a.tar"}
    one = approval.hook("s3-push", base)["rule_key"]
    require(one == approval.hook("s3-push", dict(base))["rule_key"], "rule_key stable")
    for changed in ({**base, "force": True}, {**base, "dst": "backups/b.tar"}, {**base, "src": "b.tar"},
                    {**base, "rule": "nightly"}, {**base, "unexpected": 1}):
        require(approval.hook("s3-push", changed)["rule_key"] != one, f"rule_key binds {changed}")
    require(approval.hook("s3-pull", base)["rule_key"] != approval.hook("s3-pull", {"src": "a.tar", "dst": "other"})["rule_key"],
            "pull destination bound")
    os.environ["S3_BACKUP_BUCKET"] = "other"
    require(approval.hook("s3-push", base)["rule_key"] != one, "rule_key binds the bucket")
    os.environ["S3_BACKUP_BUCKET"] = "bucket"
    message = approval.hook("s3-push", {"src": "/var/tmp/elsewhere", "dst": "x", "force": True})["message"]
    require("will be refused" in message and "overwriting" in message, message)
    message = approval.hook("s3-rm", {"prefix": "old/", "recursive": True, "confirm": True})["message"]
    require("every object under prefix old/" in message and "bucket bucket" in message, message)
    require(len(approval.hook("s3-push", {"src": "x" * 5000, "dst": "y"})["message"]) <= 450, "message bounded")
    require(approval.hook("s3-push", None)["action"] == "approve", "missing args still ask")


def text_tests():
    """Tool text the model or the user sees names no host engine."""
    seen = [ROOT / "plugin.yaml", ROOT / "skills" / "s3" / "SKILL.md"]
    texts = [p.read_text() for p in seen] + [json.dumps(s) for s in (schemas.PUSH_SCHEMA, schemas.PULL_SCHEMA, schemas.LS_SCHEMA, schemas.RM_SCHEMA)]
    require(not any("hermes" in t.lower() for t in texts), "engine named in tool text")
    for schema in (schemas.PUSH_SCHEMA, schemas.PULL_SCHEMA, schemas.LS_SCHEMA, schemas.RM_SCHEMA):
        require(set(schema) == {"name", "description", "parameters"} and schema["parameters"]["type"] == "object", "schema shape")


def rules_test():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as stream:
        stream.write("run-output:\n  prefix: backups/{date}/{year}/{month}/{day}/{ts}\n  source: ~/reports\n")
        path = stream.name
    try:
        fixed = datetime(2024, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
        value = rules.resolve("run-output", "push", path, fixed)
        require(value["local"] == os.path.expanduser("~/reports"), "rule source")
        require(value["remote"] == "backups/2024-02-03/2024/02/03/2024-02-03T04:05:06+00:00", "rule templates")
        require(rules.resolve("run-output", "pull", path, fixed) == value, "rule operation")
        try: rules.resolve("missing", "push", path, fixed)
        except KeyError: pass
        else: raise AssertionError("missing rule accepted")
    finally:
        Path(path).unlink()


def registration_test():
    sys.modules.pop("__init__", None)
    plugin = importlib.import_module("__init__")
    ctx = FakeCtx()
    plugin.register(ctx)
    names = [entry["name"] for entry in ctx.tools]
    require(names == ["s3-push", "s3-pull", "s3-ls", "s3-rm"], "tool registration")
    require(all(entry["description"] == entry["schema"]["description"] for entry in ctx.tools), "descriptions")
    require([n for n, _ in ctx.hooks] == ["pre_tool_call"], "approval hook registered")
    require(ctx.hooks[0][1]("s3-rm", {"key": "k", "confirm": True})["action"] == "approve", "registered hook gates rm")
    require(all(entry["override"] is False for entry in ctx.tools), "override policy")
    require(len(ctx.cli) == 1 and ctx.cli[0][1]["name"] == "s3", "CLI registration")
    require(len(ctx.skills) == 1 and ctx.skills[0][0] == "s3", "skill registration")
    parser = argparse.ArgumentParser()
    plugin.register_cli(parser)
    help_text = parser.format_help()
    require(all(word in help_text for word in ("push", "pull", "ls", "rm")), "CLI help")
    args = parser.parse_args(["push", "/tmp/backups", "hermes-backups/test/", "--force"])
    require(
        args.positionals == ["/tmp/backups", "hermes-backups/test/"] and args.force,
        "CLI positional push",
    )


def cli_pull_test(fake):
    from s3_backup import cli
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["S3_BACKUP_DIR"] = os.path.realpath(tmp)
        fake.objects["cli/a"] = b"a"
        require(cli.main(["pull", "cli", "restored"]) == 0, "CLI pull exit")
        require((Path(tmp) / "restored" / "a").read_bytes() == b"a", "CLI pull passes remote then local")


def main():
    os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="s3-home-")
    os.environ.update({"S3_BACKUP_BUCKET": "bucket", "S3_BACKUP_ENDPOINT": "https://invalid", "S3_BACKUP_REGION": "us-east-1", "S3_BACKUP_ACCESS_KEY": "test", "S3_BACKUP_SECRET_KEY": "test"})
    fake = FakeS3()
    original = storage._make_client
    storage._make_client = lambda cfg: fake
    tests = [("storage operations", lambda: storage_tests(fake)), ("confinement", lambda: confinement_tests(fake)),
             ("review fixes", lambda: review_fix_tests(fake)),
             ("approval", approval_tests), ("tool text", text_tests), ("rules", rules_test), ("registration", registration_test),
             ("cli pull order", lambda: cli_pull_test(fake))]
    failed = 0
    try:
        for name, test in tests:
            try:
                test()
                print(f"PASS {name}")
            except Exception as exc:
                failed += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    finally:
        storage._make_client = original
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
