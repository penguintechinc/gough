#!/usr/bin/env python3
"""Render a gough biome directory into cloud-init user-data, network-config and an LXD profile.

Shared by the hub-api and gough-install.py. Standard library only, so it runs on
bare hosts with no pip. Fails loudly on missing or invalid parameters, unknown
placeholders, and privileged profiles without explicit allow_privileged opt-in.
"""
import argparse
import base64
import json
import os
import re
import shlex
import sys

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
FORBIDDEN = re.compile(r'[\x00-\x1f\x7f"\\]')
TEMPLATES = (
    ("cloud-init/user-data.yaml.tmpl", "user-data"),
    ("cloud-init/network-config.yaml.tmpl", "network-config"),
)
REQUIRED_MANIFEST_KEYS = ("name", "version", "lxd", "parameters")
USER_DATA_HEADER = "#cloud-config\n"


class RenderError(Exception):
    """Raised for any biome validation or rendering failure; message never contains secret values."""


def load_manifest(biome_dir):
    """Load and minimally validate biome.json from a biome directory."""
    path = os.path.join(biome_dir, "biome.json")
    if not os.path.isfile(path):
        raise RenderError(f"missing manifest: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except json.JSONDecodeError as exc:
        raise RenderError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise RenderError("manifest is not an object")
    for key in REQUIRED_MANIFEST_KEYS:
        if key not in manifest:
            raise RenderError(f"manifest missing key: {key}")
    return manifest


def stringify(value):
    """Return the template text form of a resolved parameter value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def resolve_params(manifest, supplied):
    """Validate supplied parameters against the manifest schema and apply defaults."""
    if not isinstance(supplied, dict):
        raise RenderError("parameters must be a JSON object")
    insecure_ok = _insecure_flag(manifest, supplied)
    declared = {}
    for spec in manifest["parameters"]:
        name = spec.get("name")
        if not name or name in declared:
            raise RenderError(f"invalid or duplicate parameter declaration: {name!r}")
        declared[name] = spec
    unknown = sorted(set(supplied) - set(declared))
    if unknown:
        raise RenderError("unknown parameters: " + ", ".join(unknown))
    return {name: _resolve_one(name, spec, supplied, insecure_ok) for name, spec in declared.items()}


def _insecure_flag(manifest, supplied):
    """Return the allow_insecure_mirrors flag, rejecting a non-boolean value."""
    value = supplied.get("allow_insecure_mirrors", False)
    if not isinstance(value, bool):
        raise RenderError("parameter allow_insecure_mirrors must be a boolean")
    return value


def _resolve_one(name, spec, supplied, insecure_ok):
    """Return one validated value, applying the default when the parameter is absent."""
    if name in supplied:
        value = supplied[name]
    elif spec.get("required"):
        raise RenderError(f"missing required parameter: {name}")
    elif "default" in spec:
        value = spec["default"]
    else:
        raise RenderError(f"parameter {name} has no value and no default")
    ptype = spec.get("type", "string")
    if ptype == "bool":
        if not isinstance(value, bool):
            raise RenderError(f"parameter {name} must be a boolean")
        return value
    if ptype not in ("string", "enum"):
        raise RenderError(f"parameter {name} has unsupported type {ptype!r}")
    if not isinstance(value, str):
        raise RenderError(f"parameter {name} must be a string")
    if spec.get("required") and value == "":
        raise RenderError(f"parameter {name} must not be empty")
    if FORBIDDEN.search(value):
        raise RenderError(f"parameter {name} contains a control, quote or backslash character")
    if ptype == "enum" and value not in spec.get("choices", []):
        raise RenderError(f"parameter {name} is not one of the allowed choices")
    pattern = spec.get("pattern")
    if pattern and value != "" and not re.fullmatch(pattern, value):
        raise RenderError(f"parameter {name} does not match required format")
    if spec.get("scheme") == "mirror" and value and not insecure_ok and not value.startswith("https://"):
        raise RenderError(f"parameter {name} must use https (set allow_insecure_mirrors=true for plain HTTP mirrors)")
    return value


def _b64(data):
    """Return base64 text for bytes, safe to embed in YAML."""
    return base64.b64encode(data).decode("ascii")


def derived_values(biome_dir, resolved):
    """Compute template values derived from the parameters and the biome's bootstrap script."""
    env_lines = [
        f"GOUGH_{name.upper()}={shlex.quote(stringify(value))}"
        for name, value in sorted(resolved.items())
    ]
    env_text = "\n".join(env_lines) + "\n"
    with open(os.path.join(biome_dir, "scripts", "bootstrap.sh"), "rb") as fh:
        script = fh.read()
    derived = {"params_env_b64": _b64(env_text.encode("utf-8")), "bootstrap_b64": _b64(script)}
    if "dns_servers" in resolved:
        ips = resolved["dns_servers"].split(",")
        derived["dns_yaml_list"] = ", ".join(f'"{ip}"' for ip in ips)
    return derived


def render_text(text, values):
    """Substitute {{ name }} placeholders in one pass, failing on any unresolved name."""
    missing = sorted({m.group(1) for m in PLACEHOLDER.finditer(text) if m.group(1) not in values})
    if missing:
        raise RenderError("unresolved placeholders: " + ", ".join(missing))
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def build_profile(manifest, resolved, values):
    """Build the LXD profile document and enforce the privileged opt-in gate."""
    lxd = json.loads(render_text(json.dumps(manifest["lxd"]), values))
    config = {key: stringify(val) for key, val in (lxd.get("config") or {}).items()}
    if config.get("security.privileged") == "true" and resolved.get("allow_privileged") is not True:
        raise RenderError("biome requires security.privileged; set allow_privileged=true to opt in")
    name = lxd.get("profile_name")
    if not name:
        raise RenderError("manifest lxd.profile_name is required")
    return {
        "name": name,
        "image": lxd.get("image"),
        "config": config,
        "devices": lxd.get("devices") or {},
    }


def report_checks(resolved):
    """Resolve the effective report mode and enforce its parameter rules (hub needs https+token; local forbids both)."""
    if "report_mode" not in resolved:
        return
    url = resolved.get("report_url", "")
    token = resolved.get("report_bootstrap_token", "")
    mode = resolved["report_mode"] or ("hub" if url else "local")
    if mode == "hub":
        if not url.startswith("https://"):
            raise RenderError("report_mode hub requires an https report_url")
        if not token:
            raise RenderError("report_mode hub requires report_bootstrap_token")
    elif url or token:
        raise RenderError("report_mode local forbids report_url and report_bootstrap_token")
    resolved["report_mode"] = mode


CROSS_CHECKS = (report_checks,)


def render_biome(biome_dir, supplied):
    """Render user-data, network-config and profile.json for one biome instance."""
    manifest = load_manifest(biome_dir)
    resolved = resolve_params(manifest, supplied)
    for check in CROSS_CHECKS:
        check(resolved)
    values = {name: stringify(value) for name, value in resolved.items()}
    values.update(derived_values(biome_dir, resolved))
    outputs = {}
    for template, out_name in TEMPLATES:
        with open(os.path.join(biome_dir, template), encoding="utf-8") as fh:
            rendered = render_text(fh.read(), values)
        if out_name == "user-data" and not rendered.startswith(USER_DATA_HEADER):
            raise RenderError("user-data must start with the #cloud-config header")
        outputs[out_name] = rendered
    profile = build_profile(manifest, resolved, values)
    outputs["profile.json"] = json.dumps(profile, indent=2, sort_keys=True) + "\n"
    return outputs


def main(argv=None):
    """CLI entry: render a biome with a JSON parameter file into an output directory."""
    parser = argparse.ArgumentParser(description="Render a gough biome for one instance.")
    parser.add_argument("--biome", required=True, help="biome directory, e.g. biomes/k8s-worker")
    parser.add_argument("--params", required=True, help="JSON parameter file, or - for stdin")
    parser.add_argument("--out", required=True, help="output directory (created if missing)")
    args = parser.parse_args(argv)
    try:
        if args.params == "-":
            raw = sys.stdin.read()
        else:
            with open(args.params, encoding="utf-8") as fh:
                raw = fh.read()
        outputs = render_biome(args.biome, json.loads(raw))
    except (RenderError, json.JSONDecodeError, OSError) as exc:
        print(f"render failed: {exc}", file=sys.stderr)
        return 1
    os.makedirs(args.out, exist_ok=True)
    for name, text in outputs.items():
        fd = os.open(os.path.join(args.out, name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
