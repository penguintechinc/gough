---
name: gough-commit-lint-quirks
description: gough repo commit + lint workflow quirks (pre-commit no-config, non-blocking lint, penguin-dal DSL)
metadata:
  type: reference
---

Workflow quirks in the `gough` repo (as of 2026-08):

- **Committing requires `PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit ...`** — the `pre-commit` framework hook is installed but there's no `.pre-commit-config.yaml`, so a bare `git commit` errors out. Not a real gate; just prepend the env var.
- **Makefile `lint` is non-blocking** (`... || true`): `flake8 . --max-line-length=120 --ignore=E501`, `black --check .` (default 88), `mypy . --ignore-missing-imports`. The repo is NOT black-clean (untouched files like `app/api/nodes.py` also "would reformat") and carries ~2000+ pre-existing mypy errors. Match existing style; don't reformat whole files.
- **penguin-dal query DSL needs `field == False`** (builds a Query expression), NOT `is False`/`not field` — so flake8 E712 is expected and correct throughout; don't "fix" it.
- **Requirements**: `services/api-manager/requirements.txt` is a ~2200-line `uv pip compile --generate-hashes` lockfile. To add a dep without churning every transitive pin, compile with `--no-upgrade` writing to a COPY of the existing file (uv uses it as preferences), then diff.
- `app/auth/__init__.py` (package) is the live auth blueprint; `app/auth.py` (module) is shadowed/dead.

Related: [[gough-api-manager-auth]].
