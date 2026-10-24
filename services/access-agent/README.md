# gough-agent

Hypervisor access agent (rssh server + hub enrollment/heartbeat). Installed on bare hosts by the gough installer into `/opt/gough/agent/venv` and run by `gough-agent.service`.

## Install

```bash
python3 -m venv /opt/gough/agent/venv
/opt/gough/agent/venv/bin/python3 -m pip install gough-agent==<VERSION> --index-url <PYPI_MIRROR>
/opt/gough/agent/venv/bin/gough-agent --version
```

- Requires Python 3.13+.
- Console script: `gough-agent` (`agent.main:run`). `--help` and `--version` exit without starting the agent.
- Configured only through `GOUGH_*` environment variables (`/etc/gough-agent/agent.env`, no secrets). Enrollment uses a join token obtained by the installer, never stored in the env file.

## Airgapped: load the wheel into your PyPI mirror

Release assets are attached to each GitHub release by `.github/workflows/gough-agent-release.yml`:
`gough_agent-<VERSION>-py3-none-any.whl`, `gough_agent-<VERSION>.tar.gz`, `SHA256SUMS`.

```bash
# 1. Download and verify on a connected host
gh release download <RELEASE_TAG> --repo penguintechinc/gough --pattern 'gough_agent-*' --pattern SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS

# 2. Copy to the airgapped side, then publish to the mirror (example: devpi)
devpi upload --from-dir ./ --only-pypi gough_agent-<VERSION>-py3-none-any.whl

# 3. Or drop into a PEP 503 simple index directory (example: pypiserver/static layout)
mkdir -p /srv/pypi/gough-agent
cp gough_agent-<VERSION>-py3-none-any.whl /srv/pypi/gough-agent/
```

Then point the installer at the mirror: `--pypi-mirror https://<mirror>/simple --agent-version <VERSION>`. The installer indexes only that mirror.

## Development

```bash
uv venv -p 3.13 .venv && source .venv/bin/activate
uv pip install -r requirements.txt --require-hashes
uv pip install -e "./services/access-agent[test]"   # run from repo root
cd services/access-agent && python3 -m pytest
```

Dependencies: edit `requirements.in`, then regenerate the hash-locked `requirements.txt`:

```bash
uv pip compile services/access-agent/requirements.in --python-version 3.13 --generate-hashes -o services/access-agent/requirements.txt
```

Keep the exact pins in `pyproject.toml` `dependencies` in sync with `requirements.txt`.

## Build

```bash
python3 -m build services/access-agent     # writes services/access-agent/dist/ (git-ignored)
```
