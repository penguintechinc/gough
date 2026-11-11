#!/usr/bin/env python3
"""Repo-level guard: no secret material committed under k8s/ (or charts/).

Regression: k8s default-secrets triage 2026-11-10 -- k8s/manifests/secrets.yaml
shipped base64 "changeme" credentials plus a placeholder TLS key, kustomize
overlays hardcoded DB_PASS/SECRET_KEY literals, and two Helm charts rendered a
Secret from "changeme-in-production" values defaults. Secrets must come from an
externally created Secret (ESO / Sealed Secrets / pre-created) referenced by
name only.

Run either way (no make needed):
    python3 -m pytest tests/k8s/test_no_committed_secrets.py -p no:cacheprovider -o addopts=""
    python3 tests/k8s/test_no_committed_secrets.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = [
    REPO_ROOT / "k8s",
    REPO_ROOT / "charts",
    REPO_ROOT / "infrastructure" / "k8s",
]

SECRETISH_KEY = re.compile(r"PASS|SECRET|TOKEN|KEY|SALT", re.IGNORECASE)
KNOWN_DEFAULT = re.compile(
    r"change[-_]?me|prodpass|prodsecr|devpass|devsecret|stagingpass|stagingsecret",
    re.IGNORECASE,
)
# Values-file mappings whose content is rendered straight into a Secret.
SECRET_DATA_KEYS = {"secretData", "stringData"}


def _yaml_files() -> list[Path]:
    """Every YAML file under the scan roots, sorted for stable output."""
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if root.is_dir():
            files.extend(p for p in root.rglob("*") if p.suffix in {".yaml", ".yml"})
    return sorted(files)


def _is_helm_template(path: Path) -> bool:
    """Helm templates are Go-templated text, not parseable YAML."""
    return "templates" in path.parts


def _load_docs(text: str) -> list[Any]:
    """Parse a multi-document YAML string, dropping empty documents."""
    return [d for d in yaml.safe_load_all(text) if d is not None]


def _secret_doc_problems(doc: Any, where: str) -> list[str]:
    """A Secret object must not carry data/stringData literals."""
    if not isinstance(doc, dict) or doc.get("kind") != "Secret":
        return []
    problems = []
    for field in ("data", "stringData"):
        if doc.get(field):
            name = (doc.get("metadata") or {}).get("name", "?")
            problems.append(
                f"{where}: Secret '{name}' has literal {field} ({len(doc[field])} keys)"
            )
    return problems


def _kustomization_problems(doc: dict[str, Any], where: str) -> list[str]:
    """Generator literals must never hold secret-looking keys; secretGenerators must not exist."""
    problems = []
    for gen_field in ("secretGenerator", "configMapGenerator"):
        for gen in doc.get(gen_field) or []:
            if gen_field == "secretGenerator":
                problems.append(
                    f"{where}: secretGenerator '{gen.get('name')}' generates secret data in-repo"
                )
            for lit in gen.get("literals") or []:
                key = str(lit).split("=", 1)[0]
                if SECRETISH_KEY.search(key):
                    problems.append(
                        f"{where}: {gen_field} literal key '{key}' looks secret"
                    )
    return problems


def _walk(node: Any, path: str = "") -> Iterator[tuple[str, Any, Any]]:
    """Yield (path, key, value) for every mapping entry in a nested structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            yield p, k, v
            yield from _walk(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def _values_problems(doc: Any, where: str) -> list[str]:
    """Values files: no secret-data maps, no literal secret env values, no known defaults."""
    problems = []
    for path, key, value in _walk(doc):
        if key in SECRET_DATA_KEYS and value:
            problems.append(
                f"{where}: {path} holds literal secret data ({len(value)} keys)"
            )
        if path.endswith("keystoreSecret.data") and value:
            problems.append(f"{where}: {path} holds literal key material")
        if isinstance(value, str) and KNOWN_DEFAULT.search(value):
            problems.append(f"{where}: {path} is a known default placeholder")
        if isinstance(value, list):
            for item in value:
                if (
                    isinstance(item, dict)
                    and "name" in item
                    and "value" in item
                    and SECRETISH_KEY.search(str(item["name"]))
                    and not str(item["name"]).endswith(("_PATH", "_FILE"))
                ):
                    problems.append(
                        f"{where}: {path} env '{item['name']}' has a literal value"
                    )
    return problems


def _helm_charts() -> list[Path]:
    """Chart directories (contain Chart.yaml) under the scan roots."""
    charts: list[Path] = []
    for root in SCAN_ROOTS:
        if root.is_dir():
            charts.extend(p.parent for p in root.rglob("Chart.yaml"))
    return sorted(charts)


def scan_static() -> tuple[int, list[str]]:
    """Static scan of raw manifests, kustomizations and values files. Returns (files, problems)."""
    files = [f for f in _yaml_files() if not _is_helm_template(f)]
    problems: list[str] = []
    for f in files:
        where = str(f.relative_to(REPO_ROOT))
        docs = _load_docs(f.read_text())
        for doc in docs:
            problems += _secret_doc_problems(doc, where)
            if f.name in {"kustomization.yaml", "kustomization.yml"} and isinstance(
                doc, dict
            ):
                problems += _kustomization_problems(doc, where)
            if f.name.startswith("values"):
                problems += _values_problems(doc, where)
    return len(files), problems


def scan_helm_render() -> tuple[int, list[str]]:
    """Render every chart with every values file and check rendered Secrets. Returns (renders, problems)."""
    helm = shutil.which("helm")
    if helm is None:
        raise RuntimeError(
            "helm not installed -- cannot render charts (install helm to run this gate)"
        )
    renders = 0
    problems: list[str] = []
    for chart in _helm_charts():
        for values in sorted(chart.glob("values*.yaml")):
            args = [
                helm,
                "template",
                chart.name,
                str(chart),
                "-f",
                str(chart / "values.yaml"),
            ]
            if values.name != "values.yaml":
                args += ["-f", str(values)]
            out = subprocess.run(args, capture_output=True, text=True, check=False)
            where = f"helm:{chart.relative_to(REPO_ROOT)}[{values.name}]"
            if out.returncode != 0:
                problems.append(f"{where}: render failed: {out.stderr.strip()[:200]}")
                continue
            renders += 1
            for doc in _load_docs(out.stdout):
                problems += _secret_doc_problems(doc, where)
    return renders, problems


def test_no_literal_secrets_static() -> None:
    """Raw manifests, kustomizations and values files carry no secret literals."""
    count, problems = scan_static()
    print(f"static: {count} files scanned, {len(problems)} problems")
    assert count > 0, "zero YAML files scanned -- scanner pointed at the wrong root"
    assert not problems, "\n".join(problems)


def test_no_literal_secrets_helm_render() -> None:
    """No chart renders a Secret with literal data under any values file."""
    count, problems = scan_helm_render()
    print(f"helm: {count} renders, {len(problems)} problems")
    assert count > 0, "zero charts rendered"
    assert not problems, "\n".join(problems)


def main() -> int:
    """CLI entry point: run both scans, print counts, exit non-zero on any problem."""
    s_count, s_problems = scan_static()
    h_count, h_problems = scan_helm_render()
    for p in s_problems + h_problems:
        print(f"FAIL {p}")
    print(
        f"static: {s_count} files, helm: {h_count} renders, problems: {len(s_problems) + len(h_problems)}"
    )
    if s_count == 0 or h_count == 0:
        print("FAIL zero items examined")
        return 1
    return 1 if (s_problems or h_problems) else 0


if __name__ == "__main__":
    sys.exit(main())
