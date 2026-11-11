# gough-rs

Rust rewrite of the `gough` CLI (Go source stays in `cli/gough` until parity, then this tree is renamed into place).

| Crate | Role |
|---|---|
| `gough-cli` | bin `gough`: clap tree (25 root commands, 91 leaves) and dispatch |
| `gough-core` | config + contexts, output writer, error type, exit codes |
| `gough-api` | HTTP transport only (rustls, retries, bearer injection via `TokenProvider`); no endpoint functions |
| `gough-auth`, `gough-telemetry` | placeholders for device-flow/keyring auth and OTel |

## Implemented vs fail-loud

Implemented: `version`, `config` (8 subcommands), `completion` (bash/zsh/fish/powershell). Every other leaf prints
`gough <path>: not implemented yet (server contract pending)` to stderr and exits **69**.

## Security decisions

- No `--token` (secret on argv): use `GOUGH_TOKEN` or stdin. No `--insecure`: TLS verification is mandatory; `--ca-file` adds a CA.
- No global `--tenant`/`X-Tenant-ID`: the tenant comes from the JWT. `--tenant` exists only as a query filter on `node list` and `audit list`, and `-t` on `config set-context` (display hint).
- Renamed to avoid clap long-flag collisions with the global flags: `config set-context --cluster-url` (was `--cluster`), `audit export --file` (was `--output`).
- Config: `$XDG_CONFIG_HOME/gough/config.yaml` (same layout as the Go CLI), dir 0700, file 0600, atomic writes. Precedence: flags > env (`GOUGH_CLUSTER_URL`, `GOUGH_TENANT_ID`, `GOUGH_CURRENT_CONTEXT`) > selected context > file. Unlike Go, `config use-context` takes effect.
- Output: `table|json|yaml|jsonpath=<RFC 9535>`; any other `-o` value is an error. YAML via `serde_norway` (maintained fork of serde_yaml; maintainer Christina Sorensen, Denmark).

## Exit codes

See `gough --help` (0-10 as in the Go CLI, 64 usage, 69 not implemented, 78 config).

## Gates (`make -C cli/gough-rs ...`)

`lint` (fmt + clippy -D warnings), `test`, `coverage` (>= 90% lines), `build`, `audit`, `deny`.
Set `CARGO_TARGET_DIR` to a shared cache dir to avoid per-worktree `target/`.
