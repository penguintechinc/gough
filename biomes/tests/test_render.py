"""Unit tests for biomes/_lib/render.py: validation, rendering, privileged gate and co-location."""
import base64
import copy
import json
import os
import shlex
import sys
import tempfile
import unittest

import yaml

BIOMES = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(BIOMES, "_lib"))

import render  # noqa: E402  (path inserted above)

CTL_DIR = os.path.join(BIOMES, "k8s-controller")
WRK_DIR = os.path.join(BIOMES, "k8s-worker")
ZERO_SHA = "0" * 64


def controller_params(**overrides):
    """Return a complete, valid parameter set for k8s-controller with optional overrides."""
    base = {
        "hostname": "k8s-controller-1",
        "instance_name": "k8s-controller-1",
        "static_ip": "192.168.8.41/24",
        "gateway": "192.168.8.1",
        "dns_servers": "192.168.8.1,192.168.8.2",
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
        "cilium_cli_url": "https://mirror.example.internal/cilium/cilium-cli.tar.gz",
        "cilium_cli_sha256": ZERO_SHA,
        "report_url": "https://hub.example.internal/api/v1/biomes/join-material",
        "report_bootstrap_token": "bootstraptoken0123456789",
        "allow_privileged": True,
    }
    base.update(overrides)
    return base


def worker_params(**overrides):
    """Return a complete, valid parameter set for k8s-worker with optional overrides."""
    base = {
        "hostname": "k8s-worker-1",
        "instance_name": "k8s-worker-1",
        "static_ip": "192.168.8.51/24",
        "gateway": "192.168.8.1",
        "dns_servers": "192.168.8.1",
        "join_endpoint": "192.168.8.41:6443",
        "join_token": "abcdef.0123456789abcdef",
        "join_ca_cert_hash": "sha256:" + "a" * 64,
        "apt_mirror": "https://mirror.example.internal/ubuntu",
        "k8s_apt_repo": "https://mirror.example.internal/k8s/v1.35/",
        "k8s_apt_key_url": "https://mirror.example.internal/k8s/Release.key",
        "k8s_apt_key_sha256": ZERO_SHA,
        "containerd_package_version": "2.2.0-0ubuntu1",
        "registry_mirror": "https://registry.example.internal",
        "allow_privileged": True,
    }
    base.update(overrides)
    return base


def params_env_of(outputs):
    """Decode the params.env block from rendered user-data into plain text."""
    data = yaml.safe_load(outputs["user-data"])
    return base64.b64decode(data["write_files"][0]["content"]).decode("utf-8")


def manifest_of(biome_dir):
    """Load a biome manifest directly, bypassing render, for schema-level assertions."""
    with open(os.path.join(biome_dir, "biome.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class ValidationTests(unittest.TestCase):
    """Parameter validation must reject bad input and never silently default required values."""

    def test_missing_required_parameter_fails(self):
        """A missing required parameter raises RenderError naming it."""
        params = controller_params()
        del params["static_ip"]
        with self.assertRaisesRegex(render.RenderError, "missing required parameter: static_ip"):
            render.render_biome(CTL_DIR, params)

    def test_unknown_parameter_fails(self):
        """A parameter not declared in the manifest is rejected, not ignored."""
        with self.assertRaisesRegex(render.RenderError, "unknown parameters: bogus"):
            render.render_biome(WRK_DIR, worker_params(bogus="x"))

    def test_pattern_violation_fails(self):
        """A value that does not match the declared pattern is rejected."""
        with self.assertRaisesRegex(render.RenderError, "static_ip does not match"):
            render.render_biome(CTL_DIR, controller_params(static_ip="192.168.8.41"))

    def test_quote_and_newline_injection_fails(self):
        """Values carrying quotes, backslashes or newlines are rejected before templating."""
        with self.assertRaisesRegex(render.RenderError, "control, quote or backslash"):
            render.render_biome(WRK_DIR, worker_params(hostname='x"\nruncmd: [evil]'))

    def test_empty_required_value_fails(self):
        """An empty string for a required parameter is rejected even though the pattern allows empty."""
        with self.assertRaisesRegex(render.RenderError, "must not be empty"):
            render.render_biome(WRK_DIR, worker_params(containerd_package_version=""))

    def test_bool_parameter_type_enforced(self):
        """A string where a bool is declared is rejected."""
        with self.assertRaisesRegex(render.RenderError, "must be a boolean"):
            render.render_biome(WRK_DIR, worker_params(allow_privileged="yes"))

    def test_optional_empty_default_is_accepted(self):
        """Optional parameters with an empty default render as empty env values."""
        outputs = render.render_biome(WRK_DIR, worker_params())
        self.assertIn("GOUGH_INTERNAL_CA_URL=''", params_env_of(outputs))

    def test_non_object_params_fail(self):
        """A JSON list instead of an object is rejected."""
        with self.assertRaisesRegex(render.RenderError, "must be a JSON object"):
            render.render_biome(WRK_DIR, ["not", "an", "object"])


class PrivilegedGateTests(unittest.TestCase):
    """Privileged LXD profiles must require explicit allow_privileged opt-in."""

    def test_privileged_without_opt_in_fails(self):
        """Rendering a privileged profile with allow_privileged false is refused."""
        with self.assertRaisesRegex(render.RenderError, "allow_privileged=true to opt in"):
            render.render_biome(CTL_DIR, controller_params(allow_privileged=False))

    def test_privileged_with_opt_in_renders_profile(self):
        """With allow_privileged true the profile carries privileged and the bridge device."""
        profile = json.loads(render.render_biome(CTL_DIR, controller_params())["profile.json"])
        self.assertEqual(profile["config"]["security.privileged"], "true")
        self.assertEqual(profile["devices"]["eth0"]["parent"], "br0")
        self.assertEqual(profile["image"], "ubuntu/26.04")

    def test_image_alias_overridable(self):
        """The image alias parameter flows into the profile document."""
        profile = json.loads(render.render_biome(WRK_DIR, worker_params(image_alias="ubuntu/24.04"))["profile.json"])
        self.assertEqual(profile["image"], "ubuntu/24.04")


class RenderOutputTests(unittest.TestCase):
    """Rendered artifacts must be complete, parseable and free of unresolved placeholders."""

    def test_controller_renders_all_outputs(self):
        """Controller render yields user-data, network-config and profile with no leftover placeholders."""
        outputs = render.render_biome(CTL_DIR, controller_params())
        self.assertEqual(set(outputs), {"user-data", "network-config", "profile.json"})
        for name, text in outputs.items():
            self.assertNotIn("{{", text, name)

    def test_user_data_delivers_bootstrap_and_params(self):
        """User-data writes params.env and bootstrap.sh, and the bootstrap matches the file on disk."""
        outputs = render.render_biome(WRK_DIR, worker_params())
        data = yaml.safe_load(outputs["user-data"])
        paths = {f["path"]: f for f in data["write_files"]}
        self.assertEqual(set(paths), {"/opt/gough/biome/params.env", "/opt/gough/biome/bootstrap.sh"})
        with open(os.path.join(WRK_DIR, "scripts", "bootstrap.sh"), "rb") as fh:
            self.assertEqual(base64.b64decode(paths["/opt/gough/biome/bootstrap.sh"]["content"]), fh.read())

    def test_params_env_is_shell_safe(self):
        """Every params.env value round-trips through shlex, so bash sources it without injection."""
        outputs = render.render_biome(CTL_DIR, controller_params())
        data = yaml.safe_load(outputs["user-data"])
        env = base64.b64decode(data["write_files"][0]["content"]).decode("utf-8")
        parsed = {}
        for line in env.strip().splitlines():
            key, _, value = line.partition("=")
            parsed[key] = shlex.split(value)[0] if value else ""
        self.assertEqual(parsed["GOUGH_HOSTNAME"], "k8s-controller-1")
        self.assertEqual(parsed["GOUGH_REPORT_BOOTSTRAP_TOKEN"], "bootstraptoken0123456789")

    def test_network_config_is_per_instance_static(self):
        """Network-config carries exactly the static address, gateway and DNS supplied."""
        net = yaml.safe_load(render.render_biome(WRK_DIR, worker_params())["network-config"])
        eth0 = net["ethernets"]["eth0"]
        self.assertEqual(eth0["addresses"], ["192.168.8.51/24"])
        self.assertEqual(eth0["routes"][0]["via"], "192.168.8.1")
        self.assertEqual(eth0["nameservers"]["addresses"], ["192.168.8.1"])

    def test_unresolved_template_placeholder_fails(self):
        """A template placeholder with no matching value raises instead of rendering empty."""
        with self.assertRaisesRegex(render.RenderError, "unresolved placeholders: nope"):
            render.render_text("hello {{ nope }}", {})

    def test_missing_manifest_fails(self):
        """A directory without biome.yaml is rejected loudly."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(render.RenderError, "missing manifest"):
                render.render_biome(tmp, {})

    def test_manifest_missing_key_fails(self):
        """A manifest lacking a required top-level key is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "biome.yaml"), "w", encoding="utf-8") as fh:
                fh.write("name: x\nversion: 1\n")
            with self.assertRaisesRegex(render.RenderError, "missing key: lxd"):
                render.render_biome(tmp, {})

    def test_cli_writes_files_and_fails_nonzero(self):
        """The CLI writes three files on success and returns 1 with no files on bad params."""
        with tempfile.TemporaryDirectory() as tmp:
            params_path = os.path.join(tmp, "p.json")
            with open(params_path, "w", encoding="utf-8") as fh:
                json.dump(worker_params(), fh)
            out = os.path.join(tmp, "out")
            self.assertEqual(render.main(["--biome", WRK_DIR, "--params", params_path, "--out", out]), 0)
            self.assertEqual(sorted(os.listdir(out)), ["network-config", "profile.json", "user-data"])
            bad_path = os.path.join(tmp, "bad.json")
            with open(bad_path, "w", encoding="utf-8") as fh:
                json.dump({}, fh)
            self.assertEqual(render.main(["--biome", WRK_DIR, "--params", bad_path, "--out", tmp + "/bad"]), 1)
            self.assertFalse(os.path.exists(tmp + "/bad"))


class CoLocationTests(unittest.TestCase):
    """Controller and worker must render side by side on one hypervisor without host-level collisions."""

    def test_profile_names_are_per_biome(self):
        """Each biome has its own LXD profile name, so the two never overwrite each other's config."""
        ctl = json.loads(render.render_biome(CTL_DIR, controller_params())["profile.json"])
        wrk = json.loads(render.render_biome(WRK_DIR, worker_params())["profile.json"])
        self.assertEqual(ctl["name"], "gough-k8s-controller")
        self.assertEqual(wrk["name"], "gough-k8s-worker")
        self.assertNotEqual(ctl["name"], wrk["name"])

    def test_instance_identity_comes_only_from_params(self):
        """Hostname, instance name and static IP are parameters, so two instances differ only by input."""
        ctl = render.render_biome(CTL_DIR, controller_params(hostname="a-1", instance_name="a-1", static_ip="192.168.8.60/24"))
        wrk = render.render_biome(WRK_DIR, worker_params(hostname="b-1", instance_name="b-1", static_ip="192.168.8.61/24"))
        self.assertEqual(yaml.safe_load(ctl["user-data"])["hostname"], "a-1")
        self.assertEqual(yaml.safe_load(wrk["user-data"])["hostname"], "b-1")
        self.assertIn("192.168.8.60/24", ctl["network-config"])
        self.assertIn("192.168.8.61/24", wrk["network-config"])

    def test_no_fixed_hostnames_in_templates(self):
        """Templates must not hard-code the node names that would break a second instance."""
        for biome in (CTL_DIR, WRK_DIR):
            for rel in ("cloud-init/user-data.yaml.tmpl", "cloud-init/network-config.yaml.tmpl"):
                with open(os.path.join(biome, rel), encoding="utf-8") as fh:
                    text = fh.read()
                self.assertNotIn("k8s-worker-1", text, rel)
                self.assertNotIn("k8s-controller-1", text, rel)
                self.assertNotIn("192.168.8.41", text, rel)
                self.assertNotIn("192.168.8.51", text, rel)

    def test_join_material_is_parameter_only(self):
        """The worker's join endpoint, token and hash appear only via params, never in the bundled script."""
        with open(os.path.join(WRK_DIR, "scripts", "bootstrap.sh"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertNotIn("abcdef.0123456789abcdef", script)
        self.assertNotIn("192.168.8.41:6443", script)

    def test_manifest_params_are_declared_once(self):
        """Parameter names are unique within each manifest."""
        for biome in (CTL_DIR, WRK_DIR):
            names = [p["name"] for p in manifest_of(biome)["parameters"]]
            self.assertEqual(len(names), len(set(names)), biome)


if __name__ == "__main__":
    unittest.main()
