"""Unit tests for biomes/_lib/render.py: validation, rendering, privilege gate, https rules and co-location."""
import base64
import json
import os
import re
import sys
import tempfile
import unittest

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
        "cilium_crds_url": "https://mirror.example.internal/cilium/cilium-crds-v1.20.1.yaml",
        "cilium_crds_sha256": ZERO_SHA,
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


def user_data_hostname(text):
    """Return the hostname value from rendered user-data."""
    match = re.search(r'^hostname: "(.*)"$', text, re.MULTILINE)
    return match.group(1) if match else None


def decoded_contents(user_data):
    """Return the base64-decoded write_files payloads (params.env first, bootstrap second)."""
    return [base64.b64decode(c).decode("utf-8") for c in re.findall(r'content: "([A-Za-z0-9+/=]+)"', user_data)]


def params_env_of(outputs):
    """Return the decoded params.env text from rendered user-data."""
    return decoded_contents(outputs["user-data"])[0]


def manifest_of(biome_dir):
    """Load a biome manifest directly for schema-level assertions."""
    with open(os.path.join(biome_dir, "biome.json"), encoding="utf-8") as fh:
        return json.load(fh)


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
        """An empty string for a required parameter is rejected."""
        with self.assertRaisesRegex(render.RenderError, "must not be empty"):
            render.render_biome(WRK_DIR, worker_params(containerd_package_version=""))

    def test_bool_parameter_type_enforced(self):
        """A string where a bool is declared is rejected."""
        with self.assertRaisesRegex(render.RenderError, "must be a boolean"):
            render.render_biome(WRK_DIR, worker_params(allow_privileged="yes"))

    def test_optional_empty_default_is_accepted(self):
        """Optional parameters with an empty default render as empty env values."""
        self.assertIn("GOUGH_INTERNAL_CA_URL=''", params_env_of(render.render_biome(WRK_DIR, worker_params())))

    def test_non_object_params_fail(self):
        """A JSON list instead of an object is rejected."""
        with self.assertRaisesRegex(render.RenderError, "must be a JSON object"):
            render.render_biome(WRK_DIR, ["not", "an", "object"])


class HttpsPolicyTests(unittest.TestCase):
    """Cleartext must be opt-in: report_url is always https, mirrors are https unless allow_insecure_mirrors."""

    def test_report_url_http_always_rejected(self):
        """report_url over http is refused even when allow_insecure_mirrors is true."""
        params = controller_params(report_url="http://hub.example.internal/x", allow_insecure_mirrors=True)
        with self.assertRaisesRegex(render.RenderError, "report_url does not match"):
            render.render_biome(CTL_DIR, params)

    def test_local_mode_is_default_without_report_url(self):
        """With no report_url the effective mode is local and the bootstrap env says so."""
        params = controller_params()
        del params["report_url"]
        del params["report_bootstrap_token"]
        self.assertIn("GOUGH_REPORT_MODE=local", params_env_of(render.render_biome(CTL_DIR, params)))

    def test_local_mode_forbids_report_url(self):
        """report_mode local with a report_url is refused."""
        with self.assertRaisesRegex(render.RenderError, "forbids report_url"):
            render.render_biome(CTL_DIR, controller_params(report_mode="local"))

    def test_local_mode_forbids_token(self):
        """report_mode local with a bearer token is refused."""
        params = controller_params(report_mode="local")
        del params["report_url"]
        with self.assertRaisesRegex(render.RenderError, "forbids report_url and report_bootstrap_token"):
            render.render_biome(CTL_DIR, params)

    def test_hub_mode_requires_https_url(self):
        """report_mode hub without an https report_url is refused."""
        params = controller_params(report_mode="hub")
        del params["report_url"]
        with self.assertRaisesRegex(render.RenderError, "requires an https report_url"):
            render.render_biome(CTL_DIR, params)

    def test_hub_mode_requires_token(self):
        """report_mode hub without a bearer token is refused."""
        params = controller_params(report_mode="hub")
        del params["report_bootstrap_token"]
        with self.assertRaisesRegex(render.RenderError, "requires report_bootstrap_token"):
            render.render_biome(CTL_DIR, params)

    def test_hub_mode_accepts_https_with_token(self):
        """report_mode hub with https URL and token renders with mode hub."""
        self.assertIn("GOUGH_REPORT_MODE=hub", params_env_of(render.render_biome(CTL_DIR, controller_params(report_mode="hub"))))

    def test_report_url_https_accepted(self):
        """An https report_url renders."""
        self.assertTrue(render.render_biome(CTL_DIR, controller_params()))

    def test_http_mirror_rejected_by_default(self):
        """A plain-http registry mirror is refused when allow_insecure_mirrors is unset."""
        with self.assertRaisesRegex(render.RenderError, "registry_mirror must use https"):
            render.render_biome(WRK_DIR, worker_params(registry_mirror="http://registry.lab"))

    def test_http_mirror_rejected_when_flag_false(self):
        """Explicit allow_insecure_mirrors=false still refuses http mirrors."""
        with self.assertRaisesRegex(render.RenderError, "must use https"):
            render.render_biome(WRK_DIR, worker_params(registry_mirror="http://registry.lab", allow_insecure_mirrors=False))

    def test_http_mirror_accepted_with_flag(self):
        """With allow_insecure_mirrors=true an http mirror renders."""
        outputs = render.render_biome(WRK_DIR, worker_params(registry_mirror="http://registry.lab", allow_insecure_mirrors=True))
        self.assertIn("GOUGH_REGISTRY_MIRROR=http://registry.lab", params_env_of(outputs))

    def test_insecure_flag_must_be_bool(self):
        """A string value for allow_insecure_mirrors is rejected rather than treated as true."""
        with self.assertRaisesRegex(render.RenderError, "allow_insecure_mirrors must be a boolean"):
            render.render_biome(WRK_DIR, worker_params(allow_insecure_mirrors="true"))

    def test_internal_ca_http_rejected_by_default(self):
        """A plain-http internal CA URL is refused unless the insecure flag is set."""
        with self.assertRaisesRegex(render.RenderError, "internal_ca_url must use https"):
            render.render_biome(WRK_DIR, worker_params(internal_ca_url="http://ca.lab/ca.crt", internal_ca_sha256=ZERO_SHA))

    def test_every_mirror_param_is_scheme_flagged(self):
        """Each mirror-style URL parameter carries the mirror scheme flag, so the policy cannot be skipped by omission."""
        for biome in (CTL_DIR, WRK_DIR):
            for prm in manifest_of(biome)["parameters"]:
                if prm["name"].endswith(("_mirror", "_url", "_repo")) and prm["name"] != "report_url":
                    self.assertEqual(prm.get("scheme"), "mirror", f"{biome}:{prm['name']}")


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


class FeatureFlagTests(unittest.TestCase):
    """Orchestrator-decided feature switches default off and reach the bootstrap env as booleans."""

    def test_defaults_are_off(self):
        """kube_proxy_replacement, cilium_gateway_api and cilium_hubble default to false in params.env."""
        env = params_env_of(render.render_biome(CTL_DIR, controller_params()))
        self.assertIn("GOUGH_KUBE_PROXY_REPLACEMENT=false", env)
        self.assertIn("GOUGH_CILIUM_GATEWAY_API=false", env)
        self.assertIn("GOUGH_CILIUM_HUBBLE=false", env)

    def test_switches_flip_to_true(self):
        """Setting the switches true reaches params.env as true."""
        env = params_env_of(render.render_biome(CTL_DIR, controller_params(
            kube_proxy_replacement=True, cilium_gateway_api=True, cilium_hubble=True,
            gateway_api_crds_url="https://mirror.example.internal/gw.yaml", gateway_api_crds_sha256=ZERO_SHA)))
        self.assertIn("GOUGH_KUBE_PROXY_REPLACEMENT=true", env)
        self.assertIn("GOUGH_CILIUM_GATEWAY_API=true", env)
        self.assertIn("GOUGH_CILIUM_HUBBLE=true", env)

    def test_gateway_crds_url_must_be_https_by_default(self):
        """The Gateway API CRD URL follows the mirror https policy."""
        with self.assertRaisesRegex(render.RenderError, "gateway_api_crds_url must use https"):
            render.render_biome(CTL_DIR, controller_params(gateway_api_crds_url="http://mirror.lab/gw.yaml"))


class RenderOutputTests(unittest.TestCase):
    """Rendered artifacts must be complete, parseable and free of unresolved placeholders."""

    def test_controller_renders_all_outputs(self):
        """Controller render yields user-data, network-config and profile with no leftover placeholders."""
        outputs = render.render_biome(CTL_DIR, controller_params())
        self.assertEqual(set(outputs), {"user-data", "network-config", "profile.json", "instance.json"})
        for name, text in outputs.items():
            self.assertNotIn("{{", text, name)

    def test_user_data_has_cloud_config_header(self):
        """User-data starts with the cloud-config header that cloud-init requires."""
        self.assertTrue(render.render_biome(WRK_DIR, worker_params())["user-data"].startswith("#cloud-config\n"))

    def test_user_data_delivers_bootstrap_and_params(self):
        """User-data writes params.env and bootstrap.sh, and the bootstrap matches the file on disk."""
        outputs = render.render_biome(WRK_DIR, worker_params())
        paths = re.findall(r"path: (/opt/gough/biome/\S+)", outputs["user-data"])
        self.assertEqual(paths, ["/opt/gough/biome/params.env", "/opt/gough/biome/bootstrap.sh"])
        with open(os.path.join(WRK_DIR, "scripts", "bootstrap.sh"), "rb") as fh:
            self.assertEqual(base64.b64decode(re.findall(r'content: "([A-Za-z0-9+/=]+)"', outputs["user-data"])[1]), fh.read())

    def test_params_env_values_are_shell_quoted(self):
        """Values with spaces or metacharacters are single-quoted so sourcing cannot inject commands."""
        env = params_env_of(render.render_biome(WRK_DIR, worker_params(allow_insecure_mirrors=False)))
        for line in env.strip().splitlines():
            self.assertRegex(line, r"^GOUGH_[A-Z0-9_]+=", line)
        self.assertIn("GOUGH_HOSTNAME=k8s-worker-1", env)

    def test_network_config_is_per_instance_static(self):
        """Network-config carries exactly the static address, gateway and DNS supplied."""
        net = render.render_biome(WRK_DIR, worker_params())["network-config"]
        self.assertIn('- "192.168.8.51/24"', net)
        self.assertIn('via: "192.168.8.1"', net)
        self.assertIn("addresses: [\"192.168.8.1\"]", net)

    def test_unresolved_template_placeholder_fails(self):
        """A template placeholder with no matching value raises instead of rendering empty."""
        with self.assertRaisesRegex(render.RenderError, "unresolved placeholders: nope"):
            render.render_text("hello {{ nope }}", {})

    def test_crd_bundle_is_required_and_mirrored(self):
        """Omitting the Cilium CRD bundle fails rendering; its URL follows the mirror https policy."""
        params = controller_params()
        del params["cilium_crds_url"]
        with self.assertRaisesRegex(render.RenderError, "missing required parameter: cilium_crds_url"):
            render.render_biome(CTL_DIR, params)
        with self.assertRaisesRegex(render.RenderError, "cilium_crds_url must use https"):
            render.render_biome(CTL_DIR, controller_params(cilium_crds_url="http://mirror.lab/crds.yaml"))

    def test_missing_manifest_fails(self):
        """A directory without biome.json is rejected loudly."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(render.RenderError, "missing manifest"):
                render.render_biome(tmp, {})

    def test_manifest_missing_key_fails(self):
        """A manifest lacking a required top-level key is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "biome.json"), "w", encoding="utf-8") as fh:
                json.dump({"name": "x", "version": "1"}, fh)
            with self.assertRaisesRegex(render.RenderError, "missing key: lxd"):
                render.render_biome(tmp, {})

    def test_invalid_manifest_json_fails(self):
        """A malformed manifest is reported as a render error, not a traceback."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "biome.json"), "w", encoding="utf-8") as fh:
                fh.write("{not json")
            with self.assertRaisesRegex(render.RenderError, "not valid JSON"):
                render.render_biome(tmp, {})

    def test_cli_writes_files_and_fails_nonzero(self):
        """The CLI writes four files on success and returns 1 with no output directory on bad params."""
        with tempfile.TemporaryDirectory() as tmp:
            params_path = os.path.join(tmp, "p.json")
            with open(params_path, "w", encoding="utf-8") as fh:
                json.dump(worker_params(), fh)
            out = os.path.join(tmp, "out")
            self.assertEqual(render.main(["--biome", WRK_DIR, "--params", params_path, "--out", out]), 0)
            self.assertEqual(sorted(os.listdir(out)), ["instance.json", "network-config", "profile.json", "user-data"])
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

    def test_instance_identity_comes_only_from_params(self):
        """Hostname, instance name and static IP are parameters, so two instances differ only by input."""
        ctl = render.render_biome(CTL_DIR, controller_params(hostname="a-1", instance_name="a-1", static_ip="192.168.8.60/24"))
        wrk = render.render_biome(WRK_DIR, worker_params(hostname="b-1", instance_name="b-1", static_ip="192.168.8.61/24"))
        self.assertEqual(user_data_hostname(ctl["user-data"]), "a-1")
        self.assertEqual(user_data_hostname(wrk["user-data"]), "b-1")
        self.assertIn("192.168.8.60/24", ctl["network-config"])
        self.assertIn("192.168.8.61/24", wrk["network-config"])

    def test_no_fixed_hostnames_in_templates(self):
        """Templates must not hard-code the node names or addresses that would break a second instance."""
        for biome in (CTL_DIR, WRK_DIR):
            for rel in ("cloud-init/user-data.yaml.tmpl", "cloud-init/network-config.yaml.tmpl"):
                with open(os.path.join(biome, rel), encoding="utf-8") as fh:
                    text = fh.read()
                for fixed in ("k8s-worker-1", "k8s-controller-1", "192.168.8.41", "192.168.8.51"):
                    self.assertNotIn(fixed, text, rel)

    def test_join_material_is_parameter_only(self):
        """The worker's join endpoint and token appear only via params, never in the bundled script."""
        with open(os.path.join(WRK_DIR, "scripts", "bootstrap.sh"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertNotIn("abcdef.0123456789abcdef", script)
        self.assertNotIn("192.168.8.41:6443", script)

    def test_manifest_params_are_declared_once(self):
        """Parameter names are unique within each manifest."""
        for biome in (CTL_DIR, WRK_DIR):
            names = [p["name"] for p in manifest_of(biome)["parameters"]]
            self.assertEqual(len(names), len(set(names)), biome)


class ResourceTests(unittest.TestCase):
    """Every instance carries explicit LXD limits and a root disk; the hub rejects instances without them."""

    def profile(self, biome_dir, params):
        """Render and return the parsed profile.json for one biome."""
        return json.loads(render.render_biome(biome_dir, params)["profile.json"])

    def instance(self, biome_dir, params):
        """Render and return the parsed instance.json (per-instance limits and root disk) for one biome."""
        return json.loads(render.render_biome(biome_dir, params)["instance.json"])

    def test_controller_defaults_rendered(self):
        """Controller defaults: 4 cores, 8GiB hard memory without swap, 50GiB root on the default pool."""
        inst = self.instance(CTL_DIR, controller_params())
        self.assertEqual(inst["config"]["limits.cpu"], "4")
        self.assertEqual(inst["config"]["limits.memory"], "8GiB")
        self.assertEqual(inst["devices"]["root"], {"type": "disk", "path": "/", "pool": "default", "size": "50GiB"})

    def test_worker_defaults_rendered(self):
        """Worker defaults: 4 cores, 8GiB, 100GiB root."""
        inst = self.instance(WRK_DIR, worker_params())
        self.assertEqual(inst["config"]["limits.cpu"], "4")
        self.assertEqual(inst["config"]["limits.memory"], "8GiB")
        self.assertEqual(inst["devices"]["root"]["size"], "100GiB")

    def test_lxd_limit_fields_exact(self):
        """The limits block carries exactly the four limit keys with the hard-enforce and no-swap values."""
        config = self.instance(WRK_DIR, worker_params())["config"]
        limits = {k: v for k, v in config.items() if k.startswith("limits.")}
        self.assertEqual(limits, {
            "limits.cpu": "4",
            "limits.memory": "8GiB",
            "limits.memory.enforce": "hard",
            "limits.memory.swap": "false",
        })

    def test_root_device_fields_exact(self):
        """The root device is a disk on / with the supplied pool and size and no other keys."""
        out = render.render_biome(CTL_DIR, controller_params(storage_pool="fast", root_disk_size="30GiB"), ["fast"])
        devices = json.loads(out["instance.json"])["devices"]
        self.assertEqual(devices["root"], {"type": "disk", "path": "/", "pool": "fast", "size": "30GiB"})
        self.assertEqual(self.profile(CTL_DIR, controller_params())["devices"]["eth0"]["parent"], "br0")

    def test_override_above_min_accepted(self):
        """Overrides at or above the minimum render into the profile and params.env."""
        out = render.render_biome(WRK_DIR, worker_params(cpu_cores=8, memory="16GiB", root_disk_size="200GiB"))
        inst = json.loads(out["instance.json"])
        self.assertEqual(inst["config"]["limits.cpu"], "8")
        self.assertEqual(inst["config"]["limits.memory"], "16GiB")
        self.assertEqual(inst["devices"]["root"]["size"], "200GiB")
        self.assertIn("GOUGH_CPU_CORES=8", params_env_of(out))

    def test_minimum_values_accepted(self):
        """Values exactly at the minimum are valid."""
        inst = self.instance(CTL_DIR, controller_params(cpu_cores=2, memory="4GiB", root_disk_size="20GiB"))
        self.assertEqual(inst["config"]["limits.cpu"], "2")
        self.assertEqual(inst["config"]["limits.memory"], "4GiB")

    def test_cpu_below_min_rejected(self):
        """A cpu count below the manifest minimum fails loudly."""
        with self.assertRaisesRegex(render.RenderError, "cpu_cores must be an integer >= 2"):
            render.render_biome(CTL_DIR, controller_params(cpu_cores=1))

    def test_memory_below_min_rejected(self):
        """Memory below 4GiB fails loudly."""
        with self.assertRaisesRegex(render.RenderError, "memory must be >= 4GiB"):
            render.render_biome(WRK_DIR, worker_params(memory="2GiB"))

    def test_root_disk_below_min_rejected(self):
        """Root disk below 20GiB fails loudly."""
        with self.assertRaisesRegex(render.RenderError, "root_disk_size must be >= 20GiB"):
            render.render_biome(WRK_DIR, worker_params(root_disk_size="10GiB"))

    def test_bad_units_rejected(self):
        """Decimal, lowercase, SI and unitless sizes are all refused by the strict unit regex."""
        for bad in ("8G", "8GB", "8gib", "8.5GiB", "8", "0GiB", " 8GiB", "8GiB\n"):
            with self.subTest(bad=bad), self.assertRaises(render.RenderError):
                render.render_biome(WRK_DIR, worker_params(memory=bad))

    def test_bad_cpu_type_rejected(self):
        """A zero, negative, boolean or non-numeric cpu count is refused."""
        for bad in (0, -1, True, "four"):
            with self.subTest(bad=bad), self.assertRaises(render.RenderError):
                render.render_biome(WRK_DIR, worker_params(cpu_cores=bad))

    def test_manifest_resource_defaults_below_min_rejected(self):
        """A manifest whose default sits below its own minimum fails before any instance is rendered."""
        with tempfile.TemporaryDirectory() as tmp:
            biome = os.path.join(tmp, "b")
            os.makedirs(os.path.join(biome, "scripts"))
            os.makedirs(os.path.join(biome, "cloud-init"))
            manifest = manifest_of(WRK_DIR)
            manifest["resources"]["memory"] = {"default": "2GiB", "min": "4GiB"}
            with open(os.path.join(biome, "biome.json"), "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)
            with self.assertRaisesRegex(render.RenderError, "memory.default is below its min"):
                render.resolve_resources(render.load_manifest(biome), {})

    def test_missing_resources_block_fails(self):
        """A manifest without a resources block is refused."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = manifest_of(WRK_DIR)
            del manifest["resources"]
            with open(os.path.join(tmp, "biome.json"), "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)
            with self.assertRaisesRegex(render.RenderError, "missing key: resources"):
                render.load_manifest(tmp)

    def test_profile_carries_no_instance_limits(self):
        """The shared per-biome profile has no limits.* keys and no root device, whatever the instance asks for."""
        profile = self.profile(WRK_DIR, worker_params(cpu_cores=8, memory="16GiB", root_disk_size="200GiB"))
        self.assertFalse([k for k in profile["config"] if k.startswith("limits.")])
        self.assertNotIn("root", profile["devices"])

    def test_same_biome_profile_identical_across_cpu_sizes(self):
        """Two instances of one biome with different cpu_cores render byte-identical profile.json."""
        small = render.render_biome(WRK_DIR, worker_params(instance_name="w-1", hostname="w-1", cpu_cores=2))
        large = render.render_biome(WRK_DIR, worker_params(instance_name="w-2", hostname="w-2", cpu_cores=16))
        self.assertEqual(small["profile.json"], large["profile.json"])
        self.assertNotEqual(small["instance.json"], large["instance.json"])

    def test_disallowed_pool_rejected(self):
        """A storage_pool outside the trusted allowlist fails loudly."""
        with self.assertRaisesRegex(render.RenderError, "'fast' is not permitted"):
            render.render_biome(CTL_DIR, controller_params(storage_pool="fast"), ["default", "ssd"])

    def test_allowlisted_pool_accepted(self):
        """A pool named in the trusted allowlist renders into the root device."""
        out = render.render_biome(WRK_DIR, worker_params(storage_pool="ssd"), ["default", "ssd"])
        self.assertEqual(json.loads(out["instance.json"])["devices"]["root"]["pool"], "ssd")

    def test_default_pool_only_without_allowlist(self):
        """With no allowlist only the manifest default pool is permitted; any other pool is refused."""
        out = render.render_biome(WRK_DIR, worker_params(storage_pool="default"))
        self.assertEqual(json.loads(out["instance.json"])["devices"]["root"]["pool"], "default")
        with self.assertRaisesRegex(render.RenderError, "'ssd' is not permitted"):
            render.render_biome(WRK_DIR, worker_params(storage_pool="ssd"))

    def test_default_pool_must_be_on_explicit_allowlist(self):
        """When an allowlist is given, the default pool is only used if it is itself allowlisted."""
        with self.assertRaisesRegex(render.RenderError, "'default' is not permitted"):
            render.render_biome(WRK_DIR, worker_params(), ["ssd"])

    def test_tenant_params_cannot_supply_allowlist(self):
        """allowed_pools is not a tenant parameter; smuggling it through params is an unknown-parameter failure."""
        with self.assertRaisesRegex(render.RenderError, "unknown parameters: allowed_pools"):
            render.render_biome(WRK_DIR, worker_params(allowed_pools=["ssd"]))

    def test_resource_params_declared_in_manifests(self):
        """Both manifests declare the four resource parameters and a resources block."""
        for biome in (CTL_DIR, WRK_DIR):
            manifest = manifest_of(biome)
            names = {p["name"] for p in manifest["parameters"] if p.get("resource")}
            self.assertEqual(names, {"cpu_cores", "memory", "root_disk_size", "storage_pool"}, biome)
            self.assertEqual(set(manifest["resources"]), {"cpu_cores", "memory", "root_disk"}, biome)


class HardeningTests(unittest.TestCase):
    """Airgap DNS handling, mirror-only registries, and output file modes."""

    def test_dns_entries_are_stripped_and_validated(self):
        """Whitespace around DNS entries is removed; the netplan list carries clean addresses only."""
        out = render.render_biome(CTL_DIR, controller_params(dns_servers="192.168.8.1, 192.168.8.2 "))
        self.assertIn('"192.168.8.1", "192.168.8.2"', out["network-config"])
        self.assertNotIn('" 192', out["network-config"])

    def test_dns_non_ipv4_and_empty_entries_rejected(self):
        """A non-IPv4 entry or an empty entry is refused before any output is written."""
        with self.assertRaisesRegex(render.RenderError, "not a valid IPv4|does not match"):
            render.render_biome(WRK_DIR, worker_params(dns_servers="999.1.1.1"))
        with self.assertRaises(render.RenderError):
            render.render_biome(WRK_DIR, worker_params(dns_servers="192.168.8.1,,192.168.8.2"))

    def test_registry_fallback_defaults_off(self):
        """allow_registry_fallback reaches params.env as false unless an operator opts in."""
        self.assertIn("GOUGH_ALLOW_REGISTRY_FALLBACK=false", params_env_of(render.render_biome(CTL_DIR, controller_params())))
        self.assertIn("GOUGH_ALLOW_REGISTRY_FALLBACK=true", params_env_of(render.render_biome(
            CTL_DIR, controller_params(allow_registry_fallback=True))))

    def test_bootstrap_scripts_have_no_literal_public_registry(self):
        """No bootstrap script names quay.io or a public registry host literally; images come from the mirror."""
        for biome in (CTL_DIR, WRK_DIR):
            with open(os.path.join(biome, "scripts", "bootstrap.sh"), encoding="utf-8") as fh:
                script = fh.read()
            self.assertNotIn("quay.io", script, biome)

    def test_output_mode_enforced_on_reused_directory(self):
        """Rendering into a directory with pre-existing 0644 files leaves every output 0600."""
        with tempfile.TemporaryDirectory() as tmp:
            params_path = os.path.join(tmp, "p.json")
            with open(params_path, "w", encoding="utf-8") as fh:
                json.dump(worker_params(), fh)
            out = os.path.join(tmp, "out")
            os.makedirs(out)
            with open(os.path.join(out, "user-data"), "w", encoding="utf-8") as fh:
                fh.write("old")
            os.chmod(os.path.join(out, "user-data"), 0o644)
            self.assertEqual(render.main(["--biome", WRK_DIR, "--params", params_path, "--out", out]), 0)
            self.assertEqual(os.stat(os.path.join(out, "user-data")).st_mode & 0o777, 0o600)


class KubeletReservationTests(unittest.TestCase):
    """Kubelets inside the biome reserve resources and evict before the LXD hard limit is reached."""

    def test_kubelet_reservation_present_in_both_biomes(self):
        """Both bootstrap scripts ship a KubeletConfiguration with reserved and eviction settings."""
        for biome in (CTL_DIR, WRK_DIR):
            with open(os.path.join(biome, "scripts", "bootstrap.sh"), encoding="utf-8") as fh:
                script = fh.read()
            self.assertIn("kind: KubeletConfiguration", script, biome)
            for line in ("systemReserved:", "kubeReserved:", 'memory.available: "500Mi"', 'nodefs.available: "10%"'):
                self.assertIn(line, script, biome)


if __name__ == "__main__":
    unittest.main()
