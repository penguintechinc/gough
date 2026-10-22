"""Static checks: stdlib-only renderer, bootstrap syntax and ShellCheck, shared-block drift, security properties."""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

BIOMES = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LIB_DIR = os.path.join(BIOMES, "_lib")
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


def function_body(text, name):
    """Return the source of one top-level bash function, from its header to the closing brace line."""
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}\n", text, re.DOTALL | re.MULTILINE)
    if match is None:
        raise AssertionError(f"function not found: {name}")
    return match.group(1)


class RendererStdlibTests(unittest.TestCase):
    """The renderer runs on bare hosts, so nothing under biomes/_lib may import a non-stdlib module."""

    def test_lib_imports_are_stdlib_only(self):
        """Every import in biomes/_lib resolves to the standard library; at least one file is scanned."""
        scanned = 0
        offenders = []
        for entry in sorted(os.listdir(LIB_DIR)):
            if not entry.endswith(".py"):
                continue
            scanned += 1
            tree = ast.parse(read(os.path.join(LIB_DIR, entry)), filename=entry)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                for name in names:
                    if name.split(".")[0] not in sys.stdlib_module_names:
                        offenders.append(f"{entry}: {name}")
        self.assertGreater(scanned, 0, "no files scanned under biomes/_lib")
        self.assertEqual(offenders, [], "non-stdlib imports under biomes/_lib")

    def test_manifests_are_json_not_yaml(self):
        """Each biome ships a biome.json manifest and no biome.yaml, so no YAML parser is needed."""
        for name in BIOME_NAMES:
            self.assertTrue(os.path.isfile(os.path.join(BIOMES, name, "biome.json")), name)
            self.assertFalse(os.path.exists(os.path.join(BIOMES, name, "biome.yaml")), name)
            with open(os.path.join(BIOMES, name, "biome.json"), encoding="utf-8") as fh:
                json.load(fh)


class ScriptStaticTests(unittest.TestCase):
    """Bootstrap scripts must parse, lint clean, stay in step, and keep secrets out of argv and logs."""

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

    def test_every_script_variable_is_a_declared_parameter(self):
        """Each GOUGH_* variable a script reads maps to a parameter declared in its manifest."""
        for name in BIOME_NAMES:
            with open(os.path.join(BIOMES, name, "biome.json"), encoding="utf-8") as fh:
                declared = {p["name"] for p in json.load(fh)["parameters"]}
            used = set(re.findall(r"\$\{?GOUGH_([A-Z0-9_]+)", read(script_path(name))))
            missing = sorted(v.lower() for v in used if v.lower() not in declared)
            self.assertEqual(missing, [], f"{name} reads undeclared parameters")

    def test_scripts_examined_nonzero(self):
        """The scanner really read each script: zero examined lines would be a silent pass."""
        for name in BIOME_NAMES:
            self.assertGreater(len(read(script_path(name)).splitlines()), 100, name)

    def test_fetch_verifies_before_install(self):
        """fetch_verified checks sha256 before the install step that places bytes at the destination."""
        body = function_body(read(script_path("k8s-controller")), "fetch_verified")
        self.assertLess(body.index("sha256sum"), body.index("install -m 0644"))

    def test_token_never_in_curl_argv(self):
        """No curl invocation names the bootstrap token or join token on its command line."""
        for name in BIOME_NAMES:
            for line in read(script_path(name)).splitlines():
                if "curl " in line:
                    self.assertNotIn("BOOTSTRAP_TOKEN", line, name)
                    self.assertNotIn("JOIN_TOKEN", line, name)

    def test_report_body_goes_through_file(self):
        """The join-material body is written to a temp file and passed with data-binary, not inline."""
        body = function_body(read(script_path("k8s-controller")), "controller_publish_join")
        self.assertIn('data-binary = "@%s"', body)
        self.assertNotIn("--data ", body)

    def test_kubeadm_init_output_is_root_only_and_skips_token_print(self):
        """kubeadm init writes to a 0600 file and uses --skip-token-print so the token is not logged."""
        text = read(script_path("k8s-controller"))
        self.assertIn("--skip-token-print", text)
        self.assertIn('install -m 0600 /dev/null "$out"', text)

    def test_default_bootstrap_tokens_pruned(self):
        """After reporting, every bootstrap token except the report token is deleted."""
        text = read(script_path("k8s-controller"))
        self.assertIn("kubeadm token delete", text)
        self.assertIn("prune_bootstrap_tokens \"$token\"", text)

    def test_log_file_created_root_only(self):
        """The biome log is created 0600 before tee starts appending to it."""
        for name in BIOME_NAMES:
            text = read(script_path(name))
            self.assertIn('install -m 0600 /dev/null "$LOG_FILE"', text, name)
            self.assertLess(text.index('install -m 0600 /dev/null "$LOG_FILE"'), text.index('exec > >(tee'), name)

    def test_kube_proxy_path_uses_kubeadm_config_not_configmap_edit(self):
        """Conntrack zeroing is written into the kubeadm config file, not patched into the ConfigMap afterwards."""
        text = read(script_path("k8s-controller"))
        self.assertIn("kind: KubeProxyConfiguration", text)
        self.assertNotIn("kubectl -n kube-system edit configmap", text)
        self.assertNotIn("kubectl -n kube-system patch", text)

    def test_skip_phase_only_when_kpr(self):
        """The kube-proxy addon is skipped only when kube_proxy_replacement is true."""
        text = read(script_path("k8s-controller"))
        init_body = function_body(text, "controller_init")
        self.assertIn('if [[ "$GOUGH_KUBE_PROXY_REPLACEMENT" == "true" ]]; then', init_body)
        self.assertIn("--skip-phases=addon/kube-proxy", init_body)

    def test_gateway_api_requires_crd_url_and_sha(self):
        """Enabling Gateway API without its CRD URL and checksum fails loudly."""
        body = function_body(read(script_path("k8s-controller")), "controller_cilium")
        self.assertIn("cilium_gateway_api=true requires", body)


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
