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
        body = function_body(read(script_path("k8s-controller")), "report_to_hub")
        self.assertIn('data-binary = "@%s"', body)
        self.assertNotIn("--data ", body)

    def test_local_report_is_atomic_with_marker_last(self):
        """Local mode writes the report via temp file + mv, 0600 root-owned, and the done marker after the report."""
        body = function_body(read(script_path("k8s-controller")), "report_locally")
        self.assertIn("chown root:root", body)
        self.assertLess(body.index('mv -f "$tmp" "$report"'), body.index('mv -f "$tmp" "$done"'))
        self.assertIn("/var/lib/gough/biome-report.json", body)
        self.assertIn("/var/lib/gough/biome-report.done", body)

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

    def test_cilium_crds_come_from_verified_bundle(self):
        """CRDs are fetched as one sha256-verified bundle, not read from the chart directory (the chart has none)."""
        body = function_body(read(script_path("k8s-controller")), "controller_cilium")
        self.assertIn('fetch_verified "$GOUGH_CILIUM_CRDS_URL" "$GOUGH_CILIUM_CRDS_SHA256"', body)
        self.assertNotIn("cilium/crds", body)
        self.assertLess(body.index("fetch_verified \"$GOUGH_CILIUM_CRDS_URL\""), body.index("kubectl apply --server-side"))

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


class RegistryMirrorBehaviourTests(unittest.TestCase):
    """Runs the real write_registry_mirrors function under bash to check the hosts.toml it writes."""

    def write_hosts(self, fallback):
        """Execute write_registry_mirrors with the given fallback flag and return {registry: hosts.toml text}."""
        bash = tool_or_fail("bash")
        body = function_body(read(script_path("k8s-controller")), "write_registry_mirrors")
        with tempfile.TemporaryDirectory() as tmp:
            body = body.replace("/etc/containerd/certs.d", os.path.join(tmp, "certs.d"))
            script = "set -euo pipefail\nwrite_registry_mirrors() {\n" + body + "}\nwrite_registry_mirrors\n"
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GOUGH_MIRRORED_REGISTRIES": "docker.io,quay.io",
                "GOUGH_REGISTRY_MIRROR": "https://mirror.example.internal:5000",
                "GOUGH_ALLOW_REGISTRY_FALLBACK": "true" if fallback else "false",
            }
            result = subprocess.run([bash, "-c", script], env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = {}
            for reg in ("docker.io", "quay.io"):
                with open(os.path.join(tmp, "certs.d", reg, "hosts.toml"), encoding="utf-8") as fh:
                    out[reg] = fh.read()
            return out

    def test_mirror_only_by_default(self):
        """Default hosts.toml names the mirror as server and mentions no upstream registry anywhere."""
        hosts = self.write_hosts(fallback=False)
        for reg, text in hosts.items():
            self.assertIn('server = "https://mirror.example.internal:5000"', text, reg)
            self.assertIn('[host."https://mirror.example.internal:5000"]', text, reg)
            for upstream in ("registry-1.docker.io", "docker.io", "quay.io", "registry.k8s.io", "ghcr.io"):
                self.assertNotIn(upstream, text, f"{reg} hosts.toml names {upstream}")

    def test_fallback_adds_upstream_only_when_opted_in(self):
        """With allow_registry_fallback the public upstream is the server and the mirror stays a host."""
        hosts = self.write_hosts(fallback=True)
        self.assertIn('server = "https://registry-1.docker.io"', hosts["docker.io"])
        self.assertIn('server = "https://quay.io"', hosts["quay.io"])
        self.assertIn('[host."https://mirror.example.internal:5000"]', hosts["quay.io"])


class CiliumHelmArgsTests(unittest.TestCase):
    """Executes cilium_helm_sets under bash and checks the helm arguments it prints; no public registry may appear."""

    PUBLIC = ("quay.io", "registry.k8s.io", "docker.io", "ghcr.io", "registry-1")
    REPOS = ("image.repository", "operator.image.repository", "hubble.relay.image.repository",
             "envoy.image.repository", "certgen.image.repository")
    DISABLED = ("encryption.enabled=false", "hubble.ui.enabled=false", "nodeinit.enabled=false",
                "preflight.enabled=false", "clustermesh.useAPIServer=false",
                "authentication.mutual.spire.enabled=false", "authentication.mutual.spire.install.enabled=false")

    def helm_args(self, kpr="false", hubble="false", gateway="false", mirror="https://registry.example.internal/"):
        """Return the helm argument lines produced by cilium_helm_sets for the given feature switches."""
        bash = tool_or_fail("bash")
        body = function_body(read(script_path("k8s-controller")), "cilium_helm_sets")
        script = "set -euo pipefail\ncilium_helm_sets() {\n" + body + "}\ncilium_helm_sets\n"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GOUGH_KUBE_PROXY_REPLACEMENT": kpr,
            "GOUGH_CILIUM_HUBBLE": hubble,
            "GOUGH_CILIUM_GATEWAY_API": gateway,
            "GOUGH_REGISTRY_MIRROR": mirror,
            "GOUGH_CONTROL_PLANE_ENDPOINT": "192.168.8.41:6443",
        }
        result = subprocess.run([bash, "-c", script], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [line for line in result.stdout.splitlines() if line != "--set"]

    def test_no_public_registry_in_any_feature_mode(self):
        """With every feature switch on or off, no helm argument names a public registry."""
        for kpr, hubble, gateway in (("false", "false", "false"), ("true", "true", "true")):
            args = self.helm_args(kpr, hubble, gateway)
            self.assertGreater(len(args), 20)
            for arg in args:
                for public in self.PUBLIC:
                    self.assertNotIn(public, arg, f"kpr={kpr} hubble={hubble}: {arg}")

    def test_every_enabled_image_comes_from_mirror_by_digest(self):
        """Each image repository is under the mirror host and carries a sha256 digest with useDigest."""
        args = self.helm_args()
        for key in self.REPOS:
            matches = [a for a in args if a.startswith(f"{key}=")]
            self.assertEqual(len(matches), 1, key)
            self.assertTrue(matches[0].split("=", 1)[1].startswith("registry.example.internal/cilium/"), key)
            digest_key = key.replace(".repository", ".digest")
            digest = [a for a in args if a.startswith(f"{digest_key}=")]
            self.assertEqual(len(digest), 1, digest_key)
            self.assertRegex(digest[0], r"=sha256:[0-9a-f]{64}$")
            self.assertIn(f"{key.replace('.repository', '.useDigest')}=true", args)

    def test_unused_upstream_images_are_disabled_explicitly(self):
        """Features whose chart defaults would pull images (SPIRE defaults to install=true) are set false."""
        args = self.helm_args()
        for flag in self.DISABLED:
            self.assertIn(flag, args)

    def test_removed_chart_values_are_not_passed(self):
        """enableCiliumEndpointSlice was removed in chart 1.18; the 1.20.1 chart refuses it, so it must not be set."""
        args = self.helm_args()
        self.assertFalse([a for a in args if a.startswith("enableCiliumEndpointSlice=")])

    def test_mirror_trailing_slash_is_normalised(self):
        """A mirror URL with a trailing slash yields the same repository path."""
        args = self.helm_args(mirror="https://registry.example.internal/")
        self.assertIn("image.repository=registry.example.internal/cilium/cilium", args)


if __name__ == "__main__":
    unittest.main()
