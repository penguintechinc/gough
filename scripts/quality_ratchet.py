#!/usr/bin/env python3
"""Count ratchet for lint and security gates.

Each tool's finding count is compared with a committed baseline
(.quality-baseline.json). The gate fails if any count goes up, passes if equal,
and asks for a baseline reduction when a count drops. Files that are new
relative to the base ref must be fully clean regardless of the baseline.
A tool that examines zero items is a failure, never a pass.

Subcommands: check, update, new-files, staged. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

BASELINE_FILE = ".quality-baseline.json"
DEFAULT_BASE_REF = "origin/release/v1.0.X"
SKIP_DIRS = {".venv", "venv", "node_modules", ".worktrees", ".claude", ".git"}


class GateError(Exception):
    """A tool could not run or its output could not be trusted (always a failure)."""


@dataclass
class Result:
    """Outcome of one tool: total findings, items examined, findings per file."""

    findings: int
    examined: int
    files: dict[str, int] = field(default_factory=dict)


# Commands are fixed argv lists, never run through a shell (hence the nosec marks).
def run(
    cmd: list[str], ok: tuple[int, ...] = (0, 1), stdin: str | None = None, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command, raising GateError if it is missing or exits outside `ok`."""
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=False, cwd=cwd)  # nosec B603
    except FileNotFoundError as exc:
        raise GateError(f"{cmd[0]} is not installed") from exc
    if proc.returncode not in ok:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise GateError(f"{cmd[0]} exited {proc.returncode}: {' | '.join(tail)}")
    return proc


def git_lines(*args: str) -> list[str]:
    """Return non-empty output lines of a git command."""
    return [ln for ln in run(["git", *args], ok=(0,)).stdout.splitlines() if ln]


def candidate_files() -> list[str]:
    """Tracked plus untracked-not-ignored files that exist, minus vendored dirs."""
    out = []
    for path in git_lines("ls-files", "-co", "--exclude-standard"):
        if set(Path(path).parts) & SKIP_DIRS or not Path(path).is_file():
            continue
        out.append(path)
    return sorted(set(out))


def is_test_path(path: str) -> bool:
    """True for test code, which bandit and mypy do not cover."""
    p = Path(path)
    return "tests" in p.parts or "test" in p.parts or p.name.startswith("test_") or p.name == "conftest.py"


def py_files(files: list[str]) -> list[str]:
    """Python files among `files`."""
    return [f for f in files if f.endswith(".py")]


def sh_files(files: list[str]) -> list[str]:
    """Shell scripts among `files`."""
    return [f for f in files if f.endswith(".sh")]


def docker_files(files: list[str]) -> list[str]:
    """Dockerfiles among `files`."""
    return [f for f in files if Path(f).name.startswith("Dockerfile")]


def rel(path: str) -> str:
    """Normalise a tool-reported path to repo-relative form."""
    p = Path(path)
    try:
        return str(p.resolve().relative_to(Path.cwd().resolve())) if p.is_absolute() else str(p)
    except ValueError:
        return str(p)


def need_files(name: str, files: list[str]) -> None:
    """Fail when a tool has nothing to examine."""
    if not files:
        raise GateError(f"{name}: 0 files examined")


def tool_ruff_check(files: list[str]) -> Result:
    """Count ruff lint diagnostics over the Python files."""
    files = py_files(files)
    need_files("ruff_check", files)
    proc = run(["ruff", "check", "--no-cache", "--force-exclude", "--output-format", "json", *files])
    per_file: dict[str, int] = {}
    for item in json.loads(proc.stdout or "[]"):
        key = rel(item["filename"])
        per_file[key] = per_file.get(key, 0) + 1
    return Result(sum(per_file.values()), len(files), per_file)


def tool_ruff_format(files: list[str]) -> Result:
    """Count Python files that `ruff format` would rewrite."""
    files = py_files(files)
    need_files("ruff_format", files)
    proc = run(["ruff", "format", "--check", "--no-cache", "--force-exclude", "--output-format", "json", *files])
    dirty = {rel(item["filename"]) for item in json.loads(proc.stdout or "[]")}
    return Result(len(dirty), len(files), {f: 1 for f in dirty})


def tool_mypy(files: list[str]) -> Result:
    """Count mypy errors across the repo using mypy.ini (tests are excluded there)."""
    targets = [f for f in py_files(files) if not is_test_path(f)]
    need_files("mypy", targets)
    proc = run(["mypy", ".", "--ignore-missing-imports"], ok=(0, 1))
    text = proc.stdout
    match = re.search(r"checked (\d+) source files", text) or re.search(r"no issues found in (\d+) source files", text)
    if not match:
        raise GateError("mypy: could not read the examined-file count from its summary")
    per_file: dict[str, int] = {}
    for m in re.finditer(r"^(.+?):\d+: error:", text, re.M):
        per_file[rel(m.group(1))] = per_file.get(rel(m.group(1)), 0) + 1
    return Result(sum(per_file.values()), int(match.group(1)), per_file)


def tool_bandit(files: list[str]) -> Result:
    """Count bandit findings over non-test Python files."""
    targets = [f for f in py_files(files) if not is_test_path(f)]
    need_files("bandit", targets)
    proc = run(["bandit", "-q", "-f", "json", *targets])
    data = json.loads(proc.stdout)
    per_file: dict[str, int] = {}
    for item in data["results"]:
        per_file[rel(item["filename"])] = per_file.get(rel(item["filename"]), 0) + 1
    examined = len([k for k in data["metrics"] if k != "_totals"])
    return Result(sum(per_file.values()), examined, per_file)


def tool_shellcheck(files: list[str]) -> Result:
    """Count shellcheck warnings and errors over shell scripts."""
    targets = sh_files(files)
    need_files("shellcheck", targets)
    proc = run(["shellcheck", "--severity=warning", "--shell=bash", "--format=json", *targets])
    per_file: dict[str, int] = {}
    for item in json.loads(proc.stdout or "[]"):
        per_file[rel(item["file"])] = per_file.get(rel(item["file"]), 0) + 1
    return Result(sum(per_file.values()), len(targets), per_file)


def tool_hadolint(files: list[str]) -> Result:
    """Count hadolint findings over Dockerfiles."""
    targets = docker_files(files)
    need_files("hadolint", targets)
    proc = run(["hadolint", "--format", "json", *targets])
    per_file: dict[str, int] = {}
    for item in json.loads(proc.stdout or "[]"):
        per_file[rel(item["file"])] = per_file.get(rel(item["file"]), 0) + 1
    return Result(sum(per_file.values()), len(targets), per_file)


def tool_spectral(files: list[str]) -> Result:
    """Count Spectral findings on the OpenAPI spec."""
    spec = "services/api-manager/openapi/v1.yaml"
    need_files("spectral", [f for f in files if f == spec])
    proc = run(["spectral", "lint", "--ruleset", ".spectralrc.yaml", spec, "-f", "json"], ok=(0, 1))
    return Result(len(json.loads(proc.stdout or "[]")), 1)


def tool_gitleaks(files: list[str]) -> Result:
    """Count gitleaks findings in the working tree (allowlist in .gitleaks.toml)."""
    need_files("gitleaks", files)
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        proc = run(
            [
                "gitleaks",
                "detect",
                "--source",
                ".",
                "--no-git",
                "--no-banner",
                "--redact",
                "--report-format",
                "json",
                "--report-path",
                str(report),
                "--exit-code",
                "1",
            ]
        )
        scanned = re.search(r"scanned ~(\d+) bytes", proc.stderr + proc.stdout)
        if not scanned or int(scanned.group(1)) == 0:
            raise GateError("gitleaks: 0 bytes examined")
        findings = len(json.loads(report.read_text() or "[]")) if report.exists() else 0
    return Result(findings, len(files))


def tool_pip_audit(files: list[str]) -> Result:
    """Count known-vulnerable or unauditable pins across every requirements*.txt (needs network)."""
    reqs = [f for f in files if Path(f).name.startswith("requirements") and f.endswith(".txt")]
    need_files("pip_audit", reqs)
    findings = examined = 0
    for req in reqs:
        # --no-deps/--disable-pip audits the pins as written; resolving would fail on the
        # unresolvable pins that this gate must count, not hide.
        proc = run(["pip-audit", "-r", req, "--no-deps", "--disable-pip", "-f", "json", "--progress-spinner", "off"])
        deps = json.loads(proc.stdout)["dependencies"]
        examined += len(deps)
        # A dependency pip-audit had to skip was not audited, which counts as a finding.
        findings += sum(len(d.get("vulns", [])) + (1 if "skip_reason" in d else 0) for d in deps)
    if examined == 0:
        raise GateError("pip_audit: 0 dependencies examined")
    return Result(findings, examined)


def tool_npm_audit(files: list[str]) -> Result:
    """Count npm advisories from every package-lock.json (needs network)."""
    locks = [f for f in files if Path(f).name == "package-lock.json"]
    need_files("npm_audit", locks)
    findings = examined = 0
    for lock in locks:
        proc = run(["npm", "audit", "--package-lock-only", "--json"], cwd=str(Path(lock).parent))
        meta = json.loads(proc.stdout)["metadata"]
        findings += meta["vulnerabilities"]["total"]
        examined += meta["dependencies"]["total"]
    if examined == 0:
        raise GateError("npm_audit: 0 dependencies examined")
    return Result(findings, examined)


TOOLS: dict[str, Callable[[list[str]], Result]] = {
    "ruff_check": tool_ruff_check,
    "ruff_format": tool_ruff_format,
    "mypy": tool_mypy,
    "shellcheck": tool_shellcheck,
    "hadolint": tool_hadolint,
    "spectral": tool_spectral,
    "bandit": tool_bandit,
    "pip_audit": tool_pip_audit,
    "npm_audit": tool_npm_audit,
    "gitleaks": tool_gitleaks,
}
GROUPS = {
    "lint": ["ruff_check", "ruff_format", "mypy", "shellcheck", "hadolint", "spectral"],
    "security": ["bandit", "pip_audit", "npm_audit", "gitleaks"],
}
GROUPS["all"] = GROUPS["lint"] + GROUPS["security"]
# Tools that can judge a single file; used for the new-file and staged checks.
NEW_FILE_TOOLS = ["ruff_check", "ruff_format", "shellcheck", "hadolint", "bandit"]


def load_baseline(path: Path) -> dict:
    """Read the baseline file, failing loudly when it is missing or malformed."""
    if not path.is_file():
        raise GateError(f"{path} missing: run `make quality-baseline-update` once to create it")
    return json.loads(path.read_text())


def measure(names: list[str], files: list[str]) -> tuple[dict[str, Result], list[str]]:
    """Run each named tool; return results and a list of tool failures."""
    results: dict[str, Result] = {}
    errors: list[str] = []
    for name in names:
        try:
            results[name] = TOOLS[name](files)
        except (GateError, json.JSONDecodeError, KeyError) as exc:
            errors.append(f"{name}: {exc}")
    return results, errors


def compare(baseline: dict, results: dict[str, Result]) -> tuple[list[str], list[str], list[str]]:
    """Return (failures, lowered, table rows) comparing counts to the baseline."""
    failures: list[str] = []
    lowered: list[str] = []
    rows: list[str] = []
    for name, res in results.items():
        if res.examined <= 0:
            failures.append(f"{name}: 0 items examined")
        base = baseline.get("tools", {}).get(name)
        if base is None:
            failures.append(f"{name}: no baseline entry (run `make quality-baseline-update`)")
            verdict = "NO-BASELINE"
        elif res.findings > base["findings"]:
            failures.append(f"{name}: {res.findings} findings > baseline {base['findings']} (regression)")
            verdict = "FAIL"
        elif res.findings < base["findings"]:
            lowered.append(f"{name}: {base['findings']} -> {res.findings}")
            verdict = "LOWER"
        else:
            verdict = "ok"
        rows.append(
            f"  {name:<12} examined={res.examined:<7} findings={res.findings:<6} baseline={base['findings'] if base else '-':<6} {verdict}"
        )
    return failures, lowered, rows


def added_files(base_ref: str) -> list[str]:
    """Files present in the working set but new relative to base_ref (renames excluded)."""
    run(["git", "rev-parse", "--verify", "--quiet", base_ref], ok=(0,))
    added = set(git_lines("diff", "--name-only", "--diff-filter=A", "-M", base_ref))
    added |= set(git_lines("ls-files", "-o", "--exclude-standard"))
    return sorted(f for f in added if Path(f).is_file() and not set(Path(f).parts) & SKIP_DIRS)


def check_new_files(base_ref: str) -> list[str]:
    """Return failures for added files that have any finding (zero tolerance)."""
    new = added_files(base_ref)
    failures: list[str] = []
    print(f"new files vs {base_ref}: {len(new)} examined")
    if not new:
        return failures
    for name in NEW_FILE_TOOLS:
        subset = {
            "ruff_check": py_files,
            "ruff_format": py_files,
            "bandit": py_files,
            "shellcheck": sh_files,
            "hadolint": docker_files,
        }[name](new)
        if name == "bandit":
            subset = [f for f in subset if not is_test_path(f)]
        if not subset:
            continue
        try:
            res = TOOLS[name](subset)
        except (GateError, json.JSONDecodeError, KeyError) as exc:
            failures.append(f"new files {name}: {exc}")
            continue
        for path, count in sorted(res.files.items()):
            failures.append(f"new file {path}: {count} {name} finding(s); new files must be clean")
    new_py = [f for f in py_files(new) if not is_test_path(f)]
    if new_py:
        try:
            for path, count in sorted(tool_mypy_files(new_py).items()):
                failures.append(f"new file {path}: {count} mypy error(s); new files must be clean")
        except GateError as exc:
            failures.append(f"new files mypy: {exc}")
    return failures


def tool_mypy_files(files: list[str]) -> dict[str, int]:
    """Run mypy on specific files and return error counts for only those files."""
    proc = run(["mypy", "--ignore-missing-imports", *files], ok=(0, 1))
    if (
        not re.search(r"(checked|found no issues in|no issues found in) ?\d* ?source file", proc.stdout)
        and proc.returncode
    ):
        raise GateError("mypy: unreadable output")
    wanted = set(files)
    per_file: dict[str, int] = {}
    for m in re.finditer(r"^(.+?):\d+: error:", proc.stdout, re.M):
        key = rel(m.group(1))
        if key in wanted:
            per_file[key] = per_file.get(key, 0) + 1
    return per_file


def tool_names(args: argparse.Namespace) -> list[str]:
    """Tools to run: an explicit --tools list, else the selected group."""
    if args.tools:
        names = args.tools.split(",")
        unknown = [n for n in names if n not in TOOLS]
        if unknown:
            raise GateError(f"unknown tool(s): {', '.join(unknown)}")
        return names
    return GROUPS[args.group]


def cmd_check(args: argparse.Namespace) -> int:
    """Compare current counts to the baseline and enforce clean new files."""
    baseline = load_baseline(Path(args.baseline))
    files = candidate_files()
    results, errors = measure(tool_names(args), files)
    failures, lowered, rows = compare(baseline, results)
    failures = errors + failures
    print(f"quality ratchet [{args.group}] (baseline {args.baseline})")
    print("\n".join(rows))
    if not args.skip_new_files:
        failures += check_new_files(args.base_ref or baseline.get("base_ref", DEFAULT_BASE_REF))
    if lowered:
        print("counts DECREASED - lower the baseline with `make quality-baseline-update`:")
        print("\n".join(f"  {ln}" for ln in lowered))
    if failures:
        print("GATE FAILED:")
        print("\n".join(f"  {ln}" for ln in failures))
        return 1
    print("GATE PASSED")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """Write the baseline: create missing entries, lower existing ones, never raise."""
    path = Path(args.baseline)
    baseline = (
        json.loads(path.read_text()) if path.is_file() else {"version": 1, "base_ref": DEFAULT_BASE_REF, "tools": {}}
    )
    results, errors = measure(tool_names(args), candidate_files())
    if errors:
        print("cannot update baseline, tools failed:\n" + "\n".join(f"  {e}" for e in errors))
        return 1
    for name, res in results.items():
        if res.examined <= 0:
            print(f"cannot update baseline: {name} examined 0 items")
            return 1
        old = baseline["tools"].get(name)
        if old is None or res.findings < old["findings"]:
            print(f"  {name}: {old['findings'] if old else '(new)'} -> {res.findings}")
            baseline["tools"][name] = {"findings": res.findings, "examined": res.examined}
        elif res.findings > old["findings"]:
            print(f"  {name}: {res.findings} is ABOVE baseline {old['findings']}; not raised (fix the regression)")
        else:
            print(f"  {name}: unchanged at {res.findings}")
    path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
    return 0


def cmd_new_files(args: argparse.Namespace) -> int:
    """Run only the zero-tolerance check on files added relative to the base ref."""
    failures = check_new_files(args.base_ref or DEFAULT_BASE_REF)
    print("\n".join(failures) if failures else "new files clean")
    return 1 if failures else 0


def staged_entries() -> list[tuple[str, str, str]]:
    """Staged (status, path, old_path) entries for added/copied/modified/renamed files."""
    raw = run(["git", "diff", "--cached", "--name-status", "-M", "--diff-filter=ACMR", "-z"], ok=(0,)).stdout
    parts = [p for p in raw.split("\0") if p]
    entries = []
    i = 0
    while i < len(parts):
        status = parts[i][0]
        if status in "RC":
            entries.append((status, parts[i + 2], parts[i + 1]))
            i += 3
        else:
            entries.append((status, parts[i + 1], parts[i + 1]))
            i += 2
    return entries


def blob_findings(rev: str, path: str) -> int | None:
    """Findings for one blob (`:path` staged or `HEAD:path`), or None if it has no checker."""
    blob = run(["git", "show", f"{rev}:{path}"], ok=(0,)).stdout
    if path.endswith(".py"):
        check = run(
            ["ruff", "check", "--no-cache", "--output-format", "json", "--stdin-filename", path, "-"], stdin=blob
        )
        fmt = run(["ruff", "format", "--check", "--no-cache", "--stdin-filename", path, "-"], stdin=blob)
        return len(json.loads(check.stdout or "[]")) + (1 if fmt.returncode else 0)
    if path.endswith(".sh"):
        proc = run(["shellcheck", "--severity=warning", "--shell=bash", "--format=json", "-"], stdin=blob)
        return len(json.loads(proc.stdout or "[]"))
    return None


def cmd_staged(_args: argparse.Namespace) -> int:
    """Hook mode: staged new files must be clean; modified files must not get worse."""
    failures: list[str] = []
    examined = 0
    for status, path, old_path in staged_entries():
        try:
            now = blob_findings("", path)  # ":path" is the staged blob
            if now is None:
                continue
            examined += 1
            exists_in_head = status in "MR"
            before = blob_findings("HEAD", old_path) if exists_in_head else 0
        except GateError as exc:
            failures.append(f"{path}: {exc}")
            continue
        if before is None:
            before = 0
        if not exists_in_head and now > 0:
            failures.append(f"{path}: new file has {now} finding(s); new files must be clean")
        elif exists_in_head and now > before:
            failures.append(f"{path}: findings {before} -> {now}; modified files must not get worse")
    print(f"staged ratchet: {examined} lintable file(s) examined")
    if failures:
        print("\n".join(f"  {ln}" for ln in failures))
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, func in (
        ("check", cmd_check),
        ("update", cmd_update),
        ("new-files", cmd_new_files),
        ("staged", cmd_staged),
    ):
        p = sub.add_parser(name)
        p.set_defaults(func=func)
        p.add_argument("--baseline", default=BASELINE_FILE)
        p.add_argument("--group", choices=sorted(GROUPS), default="all")
        p.add_argument("--base-ref", default=None)
        p.add_argument("--tools", default=None, help="comma-separated tool names (overrides --group)")
        p.add_argument("--skip-new-files", action="store_true")
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except GateError as exc:
        print(f"GATE FAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
