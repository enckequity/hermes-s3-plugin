# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres
to [Semantic Versioning](https://semver.org/).

## [0.1.2] - 2026-11-25

### Security
- Local paths are confined to one backup directory (`S3_BACKUP_DIR`, default
  `plugin-data/s3/backups` in the profile home). Push and pull resolve every
  path, symlinks included, and refuse anything outside it, so a push cannot
  upload the profile's `.env` and a pull cannot write into its plugins. The
  backup directory itself may not be reached through a symlink, may not
  contain the profile home, and inside the home must sit under `plugin-data`.
- Symlinks and hard-linked files inside a pushed directory are skipped and
  reported (`skipped_links`); a hard-linked file named directly is refused.
  Pull writes a temporary file and renames it into place, so it never writes
  through a link.
- Push, pull and rm ask for approval on every call through a `pre_tool_call`
  hook. Its `rule_key` digests every argument, what a named rule resolves to,
  and the endpoint and bucket, so an "always" answer covers only that exact
  call.
- `force`, `recursive` and `confirm` take effect only when they are `true`.

### Fixed
- Tool schemas carry `name`, `description` and `parameters`, so models see
  the tools' arguments.
- Pull and recursive rm of prefix `p` no longer include sibling keys such as
  `p-old/x`.
- `s3 pull` on the command line passed local and remote in the wrong order;
  `--src`/`--dst` now follow each command's direction, explicit paths
  override a rule, and `--rule` no longer fails when loaded as a package.
- The rules file defaults to the active profile home.

## [0.1.0] - 2026-07-18

### Added
- Standalone Hermes plugin that backs up files and directory trees to any
  S3-compatible object store (AWS S3, Cloudflare R2, MinIO, …) and
  restores, lists, or safely removes remote objects.
- Four structured tools: `s3-push`, `s3-pull`,
  `s3-ls`, `s3-rm`.
- A `hermes s3` CLI subtree (`push`, `pull`, `ls`, `rm`) with
  positional and `--local`/`--remote`/`--rule` forms.
- Named backup rules via a YAML file (`{date}`, `{ts}`, `{year}`,
  `{month}`, `{day}` placeholders) for reusable push/pull targets.
- Safe-by-default behavior: `push` skips existing objects unless `--force`;
  `pull` validates destination paths (including symlink resolution); `rm`
  requires `--confirm` (and `--recursive` for prefixes).
- Hermetic test suite (`scripts/test-s3-backup.py`) using a fake S3
  client and fake plugin context — no network or real credentials required.
- Bundled `s3` skill with usage guidance and safety rules.
- Pixel-art logo.

### Notes
- Storage uses `boto3` directly (lazy-imported, so the plugin loads
  without it installed); no AWS CLI shell-outs.
- Configuration and credentials are environment-driven; secrets belong in
  `~/.hermes/.env` or the process environment and are never committed.
