"""Tests for biome host_requirements: manifest declarations, renderer validation and host-requirements.json output.

unittest.TestCase is used (documented exception to pytest-style) because the suite's documented runner is
`python3 -m unittest discover`; pytest collects these classes too.
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

BIOMES = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(BIOMES, "_lib"))

import render
from test_render import controller_params, worker_params

CTL_DIR = os.path.join(BIOMES, "k8s-controller")
WRK_DIR = os.path.join(BIOMES, "k8s-worker")
LEGACY_MODULES = ["ip_tables", "ip6_tables", "nf_nat", "overlay", "br_netfilter"]


def read_manifest(biome_dir):
    """Load a biome manifest straight from disk."""
    with open(os.path.join(biome_dir, "biome.json"), encoding="utf-8") as fh:
        return json.load(fh)


class BiomeCopy:
    """Context manager copying one biome (and _lib) to a temp dir so a test can rewrite its manifest safely."""

    def __init__(self, biome_dir):
        """Remember the source biome directory."""
        self.src = biome_dir
        self.tmp = None
        self.path = None

    def __enter__(self):
        """Copy the biome and return self with .path set to the copy."""
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, os.path.basename(self.src))
        shutil.copytree(self.src, self.path)
        return self

    def __exit__(self, *exc):
        """Remove the temporary copy."""
        shutil.rmtree(self.tmp)

    def set_host_requirements(self, value, remove=False):
        """Rewrite the copy's host_requirements (or drop it when remove is true)."""
        manifest = read_manifest(self.path)
        if remove:
            manifest.pop("host_requirements", None)
        else:
            manifest["host_requirements"] = value
        with open(os.path.join(self.path, "biome.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)


BIOMES_UNDER_TEST = (("k8s-worker", WRK_DIR, worker_params), ("k8s-controller", CTL_DIR, controller_params))


class ManifestDeclarationTests(unittest.TestCase):
    """Each k8s biome independently declares wireguard as a host requirement and in its LXD profile."""

    def test_worker_declares_wireguard_host_requirement(self):
        """k8s-worker host_requirements.kernel_modules is exactly ['wireguard']."""
        self.assertEqual(read_manifest(WRK_DIR)["host_requirements"], {"kernel_modules": ["wireguard"]})

    def test_controller_declares_wireguard_host_requirement(self):
        """k8s-controller host_requirements.kernel_modules is exactly ['wireguard']."""
        self.assertEqual(read_manifest(CTL_DIR)["host_requirements"], {"kernel_modules": ["wireguard"]})

    def test_worker_profile_kernel_modules_keep_order_and_add_wireguard(self):
        """k8s-worker linux.kernel_modules keeps the original entries in order and appends wireguard."""
        mods = read_manifest(WRK_DIR)["lxd"]["config"]["linux.kernel_modules"].split(",")
        self.assertEqual(mods, LEGACY_MODULES + ["wireguard"])

    def test_controller_profile_kernel_modules_keep_order_and_add_wireguard(self):
        """k8s-controller linux.kernel_modules keeps the original entries in order and appends wireguard."""
        mods = read_manifest(CTL_DIR)["lxd"]["config"]["linux.kernel_modules"].split(",")
        self.assertEqual(mods, LEGACY_MODULES + ["wireguard"])

    def test_rendered_profiles_carry_wireguard(self):
        """The rendered profile.json of each biome lists wireguard in linux.kernel_modules."""
        for name, biome_dir, params in BIOMES_UNDER_TEST:
            with self.subTest(biome=name):
                profile = json.loads(render.render_biome(biome_dir, params())["profile.json"])
                self.assertIn("wireguard", profile["config"]["linux.kernel_modules"].split(","))


class RenderOutputTests(unittest.TestCase):
    """render_biome emits host-requirements.json deterministically for every biome."""

    def test_each_biome_emits_wireguard(self):
        """host-requirements.json holds the declared modules with sorted keys and a trailing newline."""
        for name, biome_dir, params in BIOMES_UNDER_TEST:
            with self.subTest(biome=name):
                text = render.render_biome(biome_dir, params())["host-requirements.json"]
                self.assertEqual(text, '{\n  "kernel_modules": [\n    "wireguard"\n  ]\n}\n')

    def test_biome_without_field_emits_empty_list(self):
        """A biome with no host_requirements renders {"kernel_modules": []}."""
        with BiomeCopy(WRK_DIR) as copy:
            copy.set_host_requirements(None, remove=True)
            text = render.render_biome(copy.path, worker_params())["host-requirements.json"]
        self.assertEqual(json.loads(text), {"kernel_modules": []})

    def test_empty_list_is_accepted(self):
        """An explicit empty kernel_modules list is valid and renders as empty."""
        with BiomeCopy(CTL_DIR) as copy:
            copy.set_host_requirements({"kernel_modules": []})
            text = render.render_biome(copy.path, controller_params())["host-requirements.json"]
        self.assertEqual(json.loads(text), {"kernel_modules": []})

    def test_cli_writes_host_requirements_0644_and_secrets_0600(self):
        """The CLI writes host-requirements.json mode 0644 while every other output stays 0600."""
        with tempfile.TemporaryDirectory() as tmp:
            params_path = os.path.join(tmp, "params.json")
            out = os.path.join(tmp, "out")
            with open(params_path, "w", encoding="utf-8") as fh:
                json.dump(worker_params(), fh)
            self.assertEqual(render.main(["--biome", WRK_DIR, "--params", params_path, "--out", out]), 0)
            modes = {n: stat.S_IMODE(os.stat(os.path.join(out, n)).st_mode) for n in os.listdir(out)}
            with open(os.path.join(out, "host-requirements.json"), encoding="utf-8") as fh:
                content = json.load(fh)
        self.assertEqual(content, {"kernel_modules": ["wireguard"]})
        self.assertEqual(modes.pop("host-requirements.json"), 0o644)
        self.assertIn("secrets.env", modes)
        self.assertGreaterEqual(len(modes), 5)
        self.assertEqual({n: m for n, m in modes.items() if m != 0o600}, {})


class ValidationTests(unittest.TestCase):
    """Malformed host_requirements fail rendering loudly; nothing is silently dropped."""

    BAD_CASES = (
        ("block not an object", ["wireguard"], "host_requirements must be an object"),
        ("missing kernel_modules", {}, "missing key: kernel_modules"),
        ("unknown key", {"kernel_modules": [], "sysctls": {}}, "unknown keys: sysctls"),
        ("list is a string", {"kernel_modules": "wireguard"}, "kernel_modules must be a list"),
        ("entry not a string", {"kernel_modules": [7]}, "entry 7 is not a valid module name"),
        ("uppercase", {"kernel_modules": ["WireGuard"]}, "'WireGuard' is not a valid module name"),
        ("hyphen", {"kernel_modules": ["wire-guard"]}, "is not a valid module name"),
        ("path traversal", {"kernel_modules": ["../evil"]}, "is not a valid module name"),
        ("empty name", {"kernel_modules": [""]}, "is not a valid module name"),
        ("too long", {"kernel_modules": ["a" * 57]}, "is not a valid module name"),
        ("duplicate", {"kernel_modules": ["wireguard", "overlay", "wireguard"]}, "duplicate entry 'wireguard'"),
    )

    def test_invalid_content_fails_render(self):
        """Every malformed host_requirements value raises RenderError with a precise message, for both biomes."""
        for name, biome_dir, params in BIOMES_UNDER_TEST:
            for label, value, message in self.BAD_CASES:
                with self.subTest(biome=name, case=label), BiomeCopy(biome_dir) as copy:
                    copy.set_host_requirements(value)
                    with self.assertRaisesRegex(render.RenderError, message):
                        render.render_biome(copy.path, params())

    def test_max_length_name_is_accepted(self):
        """A 56-character module name is the longest accepted."""
        with BiomeCopy(WRK_DIR) as copy:
            copy.set_host_requirements({"kernel_modules": ["a" * 56]})
            text = render.render_biome(copy.path, worker_params())["host-requirements.json"]
        self.assertEqual(json.loads(text), {"kernel_modules": ["a" * 56]})

    def test_cli_fails_with_no_output_on_invalid_content(self):
        """The CLI exits 1 and writes nothing when host_requirements is invalid."""
        with tempfile.TemporaryDirectory() as tmp, BiomeCopy(WRK_DIR) as copy:
            copy.set_host_requirements({"kernel_modules": ["wireguard", "wireguard"]})
            params_path = os.path.join(tmp, "params.json")
            out = os.path.join(tmp, "out")
            with open(params_path, "w", encoding="utf-8") as fh:
                json.dump(worker_params(), fh)
            self.assertEqual(render.main(["--biome", copy.path, "--params", params_path, "--out", out]), 1)
            self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
