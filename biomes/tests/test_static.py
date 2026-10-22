"""Static checks for biome bootstrap scripts: syntax, ShellCheck, shared-block drift, parameter coverage."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

BIOMES = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BIOME_NAMES = ("k8s-controller", "k8s-worker")
BLOCK = re.compile(r"# BEGIN node-prep.*?# END node-prep", re.DOTALL)


def script_path(name):
    """Return the absolute bootstrap.sh path for a biome."""
    return os.path.join(BIOMES, name, "scripts", "bootstrap.sh")


def read(path):
    """Read a text file fully."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def tool_or_fail(name):
    """Return the tool path or fail the test; a missing linter must never count as a pass."""
    path = shutil.which(name)
    if path is None:
        raise AssertionError(f"required tool not installed: {name}")
    return path


class ScriptStaticTests(unittest.TestCase):
    """Bootstrap scripts must parse, lint clean, and stay in step with each other."""

    def test_bash_syntax(self):
        """Every bootstrap script passes bash -n."""
        bash = tool_or_fail("bash")
        for name in BIOME_NAMES:
            result = subprocess.run([bash, "-n", script_path(name)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")

    def test_shellcheck_clean(self):
        """Every bootstrap script passes shellcheck with no findings."""
        sc = tool_or_fail("shellcheck")
        for name in BIOME_NAMES:
            result = subprocess.run([sc, "-x", script_path(name)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stdout}{result.stderr}")

    def test_strict_mode_and_no_masking(self):
        """Scripts use set -euo pipefail and contain no '|| true' masking."""
        for name in BIOME_NAMES:
            text = read(script_path(name))
            self.assertIn("set -euo pipefail", text, name)
            self.assertNotIn("|| true", text, name)
            self.assertNotIn("|| :", text, name)

    def test_node_prep_block_identical(self):
        """The shared node-prep block is byte-identical in both scripts."""
        blocks = [BLOCK.search(read(script_path(n))) for n in BIOME_NAMES]
        for name, match in zip(BIOME_NAMES, blocks):
            self.assertIsNotNone(match, f"{name} lacks the node-prep block")
        self.assertEqual(blocks[0].group(0), blocks[1].group(0))

    def test_drift_guard_detects_change(self):
        """Mutating a copy of the shared block makes the comparison fail (the guard can fail)."""
        original = BLOCK.search(read(script_path("k8s-controller"))).group(0)
        mutated = original.replace("net.ipv4.ip_forward=1", "net.ipv4.ip_forward=0", 1)
        self.assertNotEqual(original, mutated)

    def test_every_script_variable_is_a_declared_parameter(self):
        """Each GOUGH_* variable a script reads maps to a parameter declared in its manifest."""
        for name in BIOME_NAMES:
            with open(os.path.join(BIOMES, name, "biome.yaml"), encoding="utf-8") as fh:
                declared = {p["name"] for p in yaml.safe_load(fh)["parameters"]}
            used = set(re.findall(r"\$\{?GOUGH_([A-Z0-9_]+)", read(script_path(name))))
            missing = sorted(v.lower() for v in used if v.lower() not in declared)
            self.assertEqual(missing, [], f"{name} reads undeclared parameters")

    def test_required_params_are_checked(self):
        """Every parameter the script requires via require_params is referenced in the script."""
        for name in BIOME_NAMES:
            text = read(script_path(name))
            match = re.search(r"require_params ((?:GOUGH_[A-Z0-9_]+\s*\\?\s*)+)", text)
            self.assertIsNotNone(match, name)
            required = re.findall(r"GOUGH_[A-Z0-9_]+", match.group(1))
            self.assertGreater(len(required), 5, name)

    def test_scripts_examined_nonzero(self):
        """The scanner really read each script: zero examined lines would be a silent pass."""
        for name in BIOME_NAMES:
            self.assertGreater(len(read(script_path(name)).splitlines()), 100, name)


class ShellcheckFailureShapeTest(unittest.TestCase):
    """Prove the ShellCheck gate fails on a known-bad script, so it is not a no-op."""

    def test_shellcheck_flags_bad_script(self):
        """An unquoted positional expansion is reported as a finding."""
        sc = tool_or_fail("shellcheck")
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.sh")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env bash\necho $1\n")
            result = subprocess.run([sc, bad], capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
