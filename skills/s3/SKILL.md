# S3 backup

Use the `s3-push`, `s3-pull`, `s3-ls`, and `s3-rm` tools, or the equivalent `s3 push`, `pull`, `ls`, and `rm`
commands.

Set `S3_BACKUP_ENDPOINT`, `S3_BACKUP_REGION`, and `S3_BACKUP_BUCKET`, plus
`S3_BACKUP_ACCESS_KEY` and `S3_BACKUP_SECRET_KEY`, in the profile's secret
scope; never put them in rules, tool arguments, or files. `S3_BACKUP_RULES`
optionally points to a rules YAML file (default `s3-backup.rules.yaml` in the
profile home).

Every local path stays inside the backup directory: `S3_BACKUP_DIR`, or
`plugin-data/s3/backups` in the profile home. Relative paths are taken from it.
A path that resolves outside it, through `..` or a symlink, is refused, and
symlinks and hard-linked files inside a pushed directory are skipped. Copy what you want backed up
into the backup directory first.

Push, pull and delete each ask the user to approve that exact call. Listing does
not.

Example rule:

```yaml
project-build:
  prefix: backups/project/{date}
  source: project
```

Use `--rule project-build` to supply omitted values. Explicit `--src`/`--dst`
values override the rule. Supported placeholders are `{date}`, `{ts}`,
`{year}`, `{month}`, and `{day}`. Existing objects are skipped unless push
uses `--force`. Pull rejects absolute, parent-directory, and symlink-escaping
object paths. Deletion always needs `--confirm`; deleting a prefix also needs
`--recursive`.
