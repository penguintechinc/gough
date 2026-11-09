"""Static test: every curl invocation in the controller and worker bootstrap scripts starts with -q (no .curlrc is read)."""
import os
import re
import unittest

BIOMES = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = {
    "k8s-worker": os.path.join(BIOMES, "k8s-worker", "scripts", "bootstrap.sh"),
    "k8s-controller": os.path.join(BIOMES, "k8s-controller", "scripts", "bootstrap.sh"),
}
# Minimum curl invocations expected per script; guards against a regex that silently matches nothing.
EXPECTED_MIN = {"k8s-worker": 1, "k8s-controller": 2}
# A curl command word: line start or a shell separator before it, whitespace, then its first argument.
CURL_RE = re.compile(r"(?:^|[\s;|&(!])curl\s+(\S+)")


def curl_first_args(path):
    """Return (line number, first argument) for every curl invocation in a script, skipping comments and apt package lists."""
    found = []
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            code = line.split("#", 1)[0] if line.lstrip().startswith("#") else line
            if code.lstrip().startswith("#") or re.search(r"\bapt(-get)?\s+install\b", code):
                continue
            for match in CURL_RE.finditer(code):
                found.append((number, match.group(1)))
    return found


class CurlHardeningTest(unittest.TestCase):
    """Every curl call must pass -q first so a .curlrc in the guest can never change its behavior."""

    def test_every_curl_starts_with_q(self):
        """Assert -q is the first curl argument in both bootstrap scripts, with a non-zero denominator."""
        for name, path in SCRIPTS.items():
            calls = curl_first_args(path)
            self.assertGreaterEqual(len(calls), EXPECTED_MIN[name], f"{name}: only {len(calls)} curl call(s) examined")
            for number, first in calls:
                self.assertEqual(first, "-q", f"{name} bootstrap.sh:{number}: curl must start with -q (found {first!r})")

    def test_detector_flags_a_curl_without_q(self):
        """The extractor itself must see a bare curl: guards against an assertion that cannot fail."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as tmp:
            tmp.write("if ! curl --fail -o x y; then :; fi\n  curl -q --fail z\n# curl --bad\napt-get install -y curl gpg\n")
        try:
            calls = curl_first_args(tmp.name)
        finally:
            os.unlink(tmp.name)
        self.assertEqual(calls, [(1, "--fail"), (2, "-q")])


if __name__ == "__main__":
    unittest.main()
