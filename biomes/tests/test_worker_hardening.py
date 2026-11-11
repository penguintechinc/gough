"""Hardening tests for the k8s-worker biome: secret split, privileged gate, apt key fingerprint, SSH masking, SystemdCgroup assert."""
import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

BIOMES = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(BIOMES, "_lib"))

import render

WRK_DIR = os.path.join(BIOMES, "k8s-worker")
CTL_DIR = os.path.join(BIOMES, "k8s-controller")
SCRIPT = os.path.join(WRK_DIR, "scripts", "bootstrap.sh")
TOKEN = "abcdef.0123456789abcdef"
FINGERPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"
ZERO_SHA = "0" * 64


def worker_params(**overrides):
    """Return a complete, valid k8s-worker parameter set with optional overrides."""
    base = {
        "hostname": "k8s-worker-1",
        "instance_name": "k8s-worker-1",
        "static_ip": "192.168.8.51/24",
        "gateway": "192.168.8.1",
        "dns_servers": "192.168.8.1",
        "join_endpoint": "192.168.8.41:6443",
        "join_token": TOKEN,
        "join_ca_cert_hash": "sha256:" + "a" * 64,
        "apt_mirror": "https://mirror.example.internal/ubuntu",
        "k8s_apt_repo": "https://mirror.example.internal/k8s/v1.35/",
        "k8s_apt_key_url": "https://mirror.example.internal/k8s/Release.key",
        "k8s_apt_key_sha256": ZERO_SHA,
        "k8s_apt_key_fingerprint": FINGERPRINT,
        "containerd_package_version": "2.2.0-0ubuntu1",
        "registry_mirror": "https://registry.example.internal",
        "allow_privileged": True,
        "accept_host_root_risk": True,
    }
    base.update(overrides)
    return base


def controller_params(**overrides):
    """Return a complete k8s-controller parameter set, used only to check the shared privileged gate."""
    base = {
        "hostname": "k8s-controller-1",
        "instance_name": "k8s-controller-1",
        "static_ip": "192.168.8.41/24",
        "gateway": "192.168.8.1",
        "dns_servers": "192.168.8.1",
        "apiserver_advertise_address": "192.168.8.41",
        "control_plane_endpoint": "192.168.8.41:6443",
        "apt_mirror": "https://mirror.example.internal/ubuntu",
        "k8s_apt_repo": "https://mirror.example.internal/k8s/v1.35/",
        "k8s_apt_key_url": "https://mirror.example.internal/k8s/Release.key",
        "k8s_apt_key_sha256": ZERO_SHA,
        "containerd_package_version": "2.2.0-0ubuntu1",
        "registry_mirror": "https://registry.example.internal",
        "chart_mirror_url": "https://mirror.example.internal/helm",
        "cilium_chart_sha256": ZERO_SHA,
        "cilium_crds_url": "https://mirror.example.internal/cilium/cilium-crds-v1.20.1.yaml",
        "cilium_crds_sha256": ZERO_SHA,
        "cilium_cli_url": "https://mirror.example.internal/cilium/cilium-cli.tar.gz",
        "cilium_cli_sha256": ZERO_SHA,
        "report_url": "https://hub.example.internal/api/v1/biomes/join-material",
        "report_bootstrap_token": "bootstraptoken0123456789",
        "allow_privileged": True,
        "accept_host_root_risk": True,
    }
    base.update(overrides)
    return base


def function_body(text, name):
    """Return the body of one top-level bash function, from its header to the closing brace line."""
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}\n", text, re.DOTALL | re.MULTILINE)
    if match is None:
        raise AssertionError(f"function not found: {name}")
    return match.group(1)


def function_source(text, name):
    """Return one complete top-level bash function, header included, so it can be executed."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.DOTALL | re.MULTILINE)
    if match is None:
        raise AssertionError(f"function not found: {name}")
    return match.group(0)


def script_text():
    """Return the worker bootstrap.sh source."""
    with open(SCRIPT, encoding="utf-8") as fh:
        return fh.read()


def require_tool(name):
    """Return the tool path or fail; a missing tool must never count as a pass."""
    path = shutil.which(name)
    if path is None:
        raise AssertionError(f"required tool not installed: {name}")
    return path


def run_bash(body, env=None):
    """Run a bash snippet under strict mode with log/die helpers and return the completed process."""
    preamble = (
        "set -euo pipefail\n"
        'log() { printf "%s\\n" "$*"; }\n'
        'die() { log "FATAL: $*"; exit 1; }\n'
    )
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run(["bash", "-c", preamble + body], capture_output=True, text=True, env=merged, check=False)


def decoded_user_data_files(user_data):
    """Return the base64-decoded write_files payloads in user-data, in order."""
    return [base64.b64decode(c) for c in re.findall(r'content: "([A-Za-z0-9+/=]+)"', user_data)]


class SecretSplitTests(unittest.TestCase):
    """Secret parameters stay out of user-data and the profile and land only in secrets.env."""

    def test_token_absent_from_user_data_profile_and_instance(self):
        """The join token, raw and base64-encoded, appears in no rendered output except secrets.env."""
        outputs = render.render_biome(WRK_DIR, worker_params())
        encoded = base64.b64encode(TOKEN.encode()).decode()
        for name in ("user-data", "network-config", "profile.json", "instance.json"):
            self.assertNotIn(TOKEN, outputs[name], name)
            self.assertNotIn(encoded, outputs[name], name)
        for payload in decoded_user_data_files(outputs["user-data"]):
            self.assertNotIn(TOKEN.encode(), payload)

    def test_params_env_has_no_join_token_but_keeps_other_params(self):
        """params.env carries non-secret parameters only."""
        params_env = decoded_user_data_files(render.render_biome(WRK_DIR, worker_params())["user-data"])[0].decode()
        self.assertNotIn("GOUGH_JOIN_TOKEN", params_env)
        self.assertIn("GOUGH_JOIN_CA_CERT_HASH=sha256:", params_env)
        self.assertIn("GOUGH_HOSTNAME=k8s-worker-1", params_env)

    def test_secrets_env_carries_token(self):
        """secrets.env is produced and contains exactly the secret parameter in KEY=value form."""
        outputs = render.render_biome(WRK_DIR, worker_params())
        self.assertIn(f"GOUGH_JOIN_TOKEN={TOKEN}\n", outputs["secrets.env"])
        self.assertEqual(len(outputs["secrets.env"].strip().splitlines()), 1)

    def test_cli_writes_secrets_env_mode_0600(self):
        """Through the CLI, secrets.env is written with mode 0600 and user-data still lacks the token."""
        with tempfile.TemporaryDirectory() as tmp:
            params_path = os.path.join(tmp, "p.json")
            with open(params_path, "w", encoding="utf-8") as fh:
                json.dump(worker_params(), fh)
            out = os.path.join(tmp, "out")
            self.assertEqual(render.main(["--biome", WRK_DIR, "--params", params_path, "--out", out]), 0)
            mode = stat.S_IMODE(os.stat(os.path.join(out, "secrets.env")).st_mode)
            self.assertEqual(mode, 0o600)
            with open(os.path.join(out, "user-data"), encoding="utf-8") as fh:
                self.assertNotIn(TOKEN, fh.read())

    def test_controller_keeps_report_token_embedded(self):
        """The controller does not opt in to secrets_file, so its report token stays in params.env (no regression)."""
        outputs = render.render_biome(CTL_DIR, controller_params())
        self.assertNotIn("secrets.env", outputs)
        self.assertIn("GOUGH_REPORT_BOOTSTRAP_TOKEN=bootstraptoken0123456789", decoded_user_data_files(outputs["user-data"])[0].decode())

    def test_missing_join_token_still_fails(self):
        """A worker render without join_token fails loudly rather than emitting an empty secret."""
        params = worker_params()
        del params["join_token"]
        with self.assertRaisesRegex(render.RenderError, "missing required parameter: join_token"):
            render.render_biome(WRK_DIR, params)


class PrivilegedGateTests(unittest.TestCase):
    """Privileged LXD settings require allow_privileged and accept_host_root_risk together."""

    def test_default_worker_profile_is_unprivileged(self):
        """With allow_privileged false the worker profile omits security.privileged and raw.lxc."""
        profile = json.loads(render.render_biome(WRK_DIR, worker_params(allow_privileged=False))["profile.json"])
        self.assertNotIn("security.privileged", profile["config"])
        self.assertNotIn("raw.lxc", profile["config"])
        self.assertEqual(profile["config"]["security.nesting"], "true")

    def test_privileged_without_acknowledgement_fails(self):
        """allow_privileged true without accept_host_root_risk fails rendering with a clear message."""
        with self.assertRaisesRegex(render.RenderError, "accept_host_root_risk=true"):
            render.render_biome(WRK_DIR, worker_params(accept_host_root_risk=False))

    def test_acknowledgement_without_opt_in_stays_unprivileged(self):
        """accept_host_root_risk alone does not enable privilege."""
        profile = json.loads(render.render_biome(WRK_DIR, worker_params(allow_privileged=False))["profile.json"])
        self.assertNotIn("security.privileged", profile["config"])

    def test_both_flags_emit_privileged_settings(self):
        """With both flags true the worker profile carries security.privileged and the unconfined raw.lxc."""
        profile = json.loads(render.render_biome(WRK_DIR, worker_params())["profile.json"])
        self.assertEqual(profile["config"]["security.privileged"], "true")
        self.assertIn("lxc.apparmor.profile=unconfined", profile["config"]["raw.lxc"])

    def test_controller_privileged_without_acknowledgement_fails(self):
        """The shared gate also covers the controller's own privileged config."""
        with self.assertRaisesRegex(render.RenderError, "accept_host_root_risk=true"):
            render.render_biome(CTL_DIR, controller_params(accept_host_root_risk=False))

    def test_acknowledgement_must_be_boolean(self):
        """A non-boolean accept_host_root_risk is refused."""
        with self.assertRaisesRegex(render.RenderError, "accept_host_root_risk must be a boolean"):
            render.render_biome(WRK_DIR, worker_params(accept_host_root_risk="yes"))

    def test_root_exception_comment_at_emission_point(self):
        """render.py carries the ROOT EXCEPTION marker next to the privileged emission."""
        with open(os.path.join(BIOMES, "_lib", "render.py"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("# ROOT EXCEPTION (pending approval): lab-only", text)

    def test_default_manifest_is_unprivileged(self):
        """The worker manifest keeps privileged settings out of its default lxd.config block."""
        with open(os.path.join(WRK_DIR, "biome.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertNotIn("security.privileged", manifest["lxd"]["config"])
        self.assertNotIn("raw.lxc", manifest["lxd"]["config"])
        self.assertIn("security.privileged", manifest["lxd"]["privileged_config"])


class FingerprintParameterTests(unittest.TestCase):
    """k8s_apt_key_fingerprint is required and must be exactly 40 hex characters."""

    def test_missing_fingerprint_fails(self):
        """Omitting the fingerprint fails rendering."""
        params = worker_params()
        del params["k8s_apt_key_fingerprint"]
        with self.assertRaisesRegex(render.RenderError, "missing required parameter: k8s_apt_key_fingerprint"):
            render.render_biome(WRK_DIR, params)

    def test_short_fingerprint_rejected(self):
        """39 hex characters fail the pattern."""
        with self.assertRaisesRegex(render.RenderError, "does not match"):
            render.render_biome(WRK_DIR, worker_params(k8s_apt_key_fingerprint=FINGERPRINT[:39]))

    def test_long_fingerprint_rejected(self):
        """41 characters fail: the pattern is a full match, not a prefix match."""
        with self.assertRaisesRegex(render.RenderError, "does not match"):
            render.render_biome(WRK_DIR, worker_params(k8s_apt_key_fingerprint=FINGERPRINT + "0"))

    def test_non_hex_fingerprint_rejected(self):
        """A non-hex character fails the pattern."""
        with self.assertRaisesRegex(render.RenderError, "does not match"):
            render.render_biome(WRK_DIR, worker_params(k8s_apt_key_fingerprint=FINGERPRINT[:39] + "G"))

    def test_lowercase_fingerprint_accepted(self):
        """Lowercase hex is accepted; the script compares case-insensitively."""
        outputs = render.render_biome(WRK_DIR, worker_params(k8s_apt_key_fingerprint=FINGERPRINT.lower()))
        self.assertIn(f"GOUGH_K8S_APT_KEY_FINGERPRINT={FINGERPRINT.lower()}", decoded_user_data_files(outputs["user-data"])[0].decode())

    def test_verification_runs_before_dearmor(self):
        """The bundled script verifies the fingerprint before the key is dearmored into the keyring."""
        body = function_body(script_text(), "prep_k8s_apt")
        self.assertIn('verify_apt_key_fingerprint "$armored"', body)
        self.assertLess(body.index('verify_apt_key_fingerprint "$armored"'), body.index("--dearmor"))


class ShellBehaviourTests(unittest.TestCase):
    """Run the real bash functions from bootstrap.sh; the failure paths must exit non-zero with a clear message."""

    def test_read_secret_missing_file_fails_loudly(self):
        """A worker that needs the join token but finds no secrets file exits non-zero and says why."""
        body = function_source(script_text(), "read_secret") + 'read_secret GOUGH_JOIN_TOKEN\necho "got=$SECRET_VALUE"\n'
        with tempfile.TemporaryDirectory() as tmp:
            result = run_bash(f'SECRETS_FILE="{tmp}/secrets.env"\n' + body)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secrets file missing", result.stdout)
        self.assertNotIn("got=", result.stdout)

    def test_read_secret_wrong_mode_fails(self):
        """A secrets file that is not mode 0600 is refused."""
        body = function_source(script_text(), "read_secret") + "read_secret GOUGH_JOIN_TOKEN\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "secrets.env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"GOUGH_JOIN_TOKEN={TOKEN}\n")
            os.chmod(path, 0o644)
            result = run_bash(f'SECRETS_FILE="{path}"\n' + body)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be mode 0600", result.stdout)

    def test_read_secret_reads_rendered_value(self):
        """The rendered secrets.env value is parsed back exactly, with no sourcing."""
        body = function_source(script_text(), "read_secret") + 'read_secret GOUGH_JOIN_TOKEN\necho "got=$SECRET_VALUE"\n'
        outputs = render.render_biome(WRK_DIR, worker_params())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "secrets.env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(outputs["secrets.env"])
            os.chmod(path, 0o600)
            result = run_bash(f'SECRETS_FILE="{path}"\n' + body)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"got={TOKEN}", result.stdout)

    def test_remove_file_securely_deletes_and_tolerates_missing(self):
        """The cleanup helper deletes an existing file and returns success for a missing one."""
        body = function_source(script_text(), "remove_file_securely")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "secrets.env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x\n")
            result = run_bash(body + f'remove_file_securely "{path}"\nremove_file_securely "{path}"\necho done\n')
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse(os.path.exists(path))

    def test_mask_ssh_units_masks_present_units_only(self):
        """With allow_ssh false, only the ssh units systemctl reports are disabled and masked, and nothing else."""
        require_tool("bash")
        body = function_source(script_text(), "mask_ssh_units")
        with tempfile.TemporaryDirectory() as tmp:
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            log_path = os.path.join(tmp, "systemctl.log")
            stub = os.path.join(bindir, "systemctl")
            with open(stub, "w", encoding="utf-8") as fh:
                fh.write('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\n'
                         'if [[ "$1" == "list-unit-files" ]]; then\n'
                         '  [[ "$3" == "ssh.service" ]] && echo "ssh.service enabled enabled"\n'
                         '  exit 0\nfi\nexit 0\n')
            os.chmod(stub, 0o755)
            env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "SYSTEMCTL_LOG": log_path, "GOUGH_ALLOW_SSH": "false"}
            result = run_bash(body + "mask_ssh_units\n", env=env)
            self.assertEqual(result.returncode, 0, result.stdout)
            with open(log_path, encoding="utf-8") as fh:
                calls = fh.read().splitlines()
        self.assertIn("disable --now ssh.service", calls)
        self.assertIn("mask ssh.service", calls)
        self.assertFalse(any("ssh.socket" in c and c.startswith(("disable", "mask")) for c in calls), calls)

    def test_mask_ssh_units_skipped_when_allowed(self):
        """With allow_ssh true no disable or mask call is made."""
        body = function_source(script_text(), "mask_ssh_units")
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "systemctl.log")
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            stub = os.path.join(bindir, "systemctl")
            with open(stub, "w", encoding="utf-8") as fh:
                fh.write('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\nexit 0\n')
            os.chmod(stub, 0o755)
            env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "SYSTEMCTL_LOG": log_path, "GOUGH_ALLOW_SSH": "true"}
            result = run_bash(body + "mask_ssh_units\n", env=env)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse(os.path.exists(log_path))

    def test_verify_fingerprint_accepts_real_key_and_rejects_mismatch(self):
        """A real GPG key passes with its own fingerprint and fails with a different one, using the bundled function."""
        require_tool("gpg")
        body = function_source(script_text(), "verify_apt_key_fingerprint")
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "gnupg")
            os.makedirs(home, mode=0o700)
            genv = {"GNUPGHOME": home}
            subprocess.run(["gpg", "--batch", "--passphrase", "", "--quick-gen-key", "gough-test <test@example.invalid>",
                            "ed25519", "default", "0"], env=dict(os.environ, **genv), check=True, capture_output=True)
            listing = subprocess.run(["gpg", "--batch", "--with-colons", "--fingerprint"], env=dict(os.environ, **genv),
                                     check=True, capture_output=True, text=True).stdout
            real_fpr = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
            keyfile = os.path.join(tmp, "key.asc")
            with open(keyfile, "w", encoding="utf-8") as fh:
                fh.write(subprocess.run(["gpg", "--batch", "--armor", "--export", real_fpr],
                                        env=dict(os.environ, **genv), check=True, capture_output=True, text=True).stdout)
            ok = run_bash(body + f'verify_apt_key_fingerprint "{keyfile}"\n',
                          env=dict(os.environ, GOUGH_K8S_APT_KEY_FINGERPRINT=real_fpr.lower(), **genv))
            bad_fpr = ("0" if real_fpr[0] != "0" else "1") + real_fpr[1:]
            bad = run_bash(body + f'verify_apt_key_fingerprint "{keyfile}"\n',
                           env=dict(os.environ, GOUGH_K8S_APT_KEY_FINGERPRINT=bad_fpr, **genv))
        self.assertEqual(ok.returncode, 0, ok.stdout)
        self.assertIn("fingerprint verified", ok.stdout)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("fingerprint mismatch", bad.stdout)

    def test_systemdcgroup_count_is_asserted(self):
        """The bundled script counts SystemdCgroup = true matches and requires exactly one before restart."""
        body = function_source(script_text(), "prep_containerd")
        self.assertNotIn('grep -q \'SystemdCgroup = true\'', body)
        self.assertIn('[[ "$cgroup_count" == "1" ]]', body)
        self.assertLess(body.index('[[ "$cgroup_count" == "1" ]]'), body.index("systemctl restart containerd"))


class WorkerMainWiringTests(unittest.TestCase):
    """main() wires the new steps: SSH masking first, join token required only at join time."""

    def test_main_calls_ssh_masking(self):
        """main() invokes mask_ssh_units before node prep."""
        body = function_body(script_text(), "main")
        self.assertIn("mask_ssh_units", body)
        self.assertLess(body.index("mask_ssh_units"), body.index("run_step node-prep"))

    def test_main_does_not_require_join_token_env(self):
        """The join token is no longer a required environment variable; it is read from the secrets file."""
        body = function_body(script_text(), "main")
        self.assertNotIn("GOUGH_JOIN_TOKEN", body)

    def test_cleanup_trap_installed(self):
        """An EXIT trap removes the secrets file and join config on success and failure."""
        text = script_text()
        self.assertIn("trap cleanup_join_material EXIT", text)
        body = function_body(text, "cleanup_join_material")
        self.assertIn("remove_file_securely \"$SECRETS_FILE\"", body)
        self.assertIn("remove_file_securely \"$JOIN_CONFIG\"", body)

    def test_allow_ssh_declared_bool_default_false(self):
        """allow_ssh is a declared boolean parameter defaulting to false."""
        with open(os.path.join(WRK_DIR, "biome.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        spec = next(p for p in manifest["parameters"] if p["name"] == "allow_ssh")
        self.assertEqual(spec["type"], "bool")
        self.assertIs(spec["default"], False)


class CurlHardeningTests(unittest.TestCase):
    """Every curl invocation in the worker bootstrap starts with -q so a planted ~/.curlrc is never read."""

    def test_every_curl_invocation_has_q_first(self):
        """Count curl command lines and require `curl -q ` on each; zero examined is a failure."""
        calls = [ln.strip() for ln in script_text().splitlines()
                 if re.match(r"\s*(if ! |)curl ", ln) and not ln.lstrip().startswith("#")]
        self.assertGreaterEqual(len(calls), 1)
        for ln in calls:
            self.assertRegex(ln, r"^(if ! )?curl -q ", ln)


if __name__ == "__main__":
    unittest.main()
