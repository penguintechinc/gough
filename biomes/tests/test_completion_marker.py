"""Completion and failure markers for the k8s-worker bootstrap, exercised through its real EXIT trap."""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

BIOMES = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(BIOMES, "k8s-worker", "scripts", "bootstrap.sh")
FUNCS = ("remove_file_securely", "report_biome_outcome", "cleanup_join_material")


def function_source(text, name):
    """Return one complete top-level bash function, header included."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.DOTALL | re.MULTILINE)
    if match is None:
        raise AssertionError(f"function not found: {name}")
    return match.group(0)


class CompletionMarkerTests(unittest.TestCase):
    """The worker writes biome-report.done on success and biome-report.failed on any exit, after secrets are removed."""

    def setUp(self):
        """Create a private temp tree holding a fake secrets file and join config."""
        if shutil.which("bash") is None:
            raise AssertionError("required tool not installed: bash")
        self.tmp = tempfile.mkdtemp(prefix="marker-")
        self.report = os.path.join(self.tmp, "report")
        self.secrets = os.path.join(self.tmp, "secrets.env")
        self.join = os.path.join(self.tmp, "join.yaml")
        with open(self.secrets, "w", encoding="utf-8") as fh:
            fh.write("GOUGH_JOIN_TOKEN='abcdef.0123456789abcdef'\n")
        with open(self.join, "w", encoding="utf-8") as fh:
            fh.write("token: abcdef.0123456789abcdef\n")
        with open(SCRIPT, encoding="utf-8") as fh:
            self.text = fh.read()

    def tearDown(self):
        """Remove the temp tree."""
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_exit(self, code):
        """Run the real trap in a bash process that exits with code, after failing inside a named step."""
        body = "\n".join(function_source(self.text, f) for f in FUNCS)
        driver = (
            "set -euo pipefail\n" + body + "\n"
            f"REPORT_DIR='{self.report}'\n"
            f"SECRETS_FILE='{self.secrets}'\n"
            f"JOIN_CONFIG='{self.join}'\n"
            "CURRENT_STEP=kubeadm-join\n"
            "trap cleanup_join_material EXIT\n"
            f"exit {code}\n"
        )
        return subprocess.run(["bash", "-c", driver], capture_output=True, text=True, check=False)

    def test_success_writes_done_and_removes_secrets(self):
        """Exit 0 writes biome-report.done (mode 0600), no failed marker, and removes the secrets file first."""
        res = self.run_exit(0)
        self.assertEqual(res.returncode, 0, res.stderr)
        done = os.path.join(self.report, "biome-report.done")
        self.assertTrue(os.path.isfile(done))
        self.assertFalse(os.path.exists(os.path.join(self.report, "biome-report.failed")))
        self.assertEqual(stat.S_IMODE(os.stat(done).st_mode), 0o600)
        self.assertFalse(os.path.exists(self.secrets), "secrets file must be gone before the marker")
        self.assertFalse(os.path.exists(self.join))

    def test_failure_writes_failed_reason_without_secret(self):
        """A non-zero exit writes biome-report.failed naming the step and code, never the token."""
        res = self.run_exit(3)
        self.assertEqual(res.returncode, 3)
        failed = os.path.join(self.report, "biome-report.failed")
        self.assertTrue(os.path.isfile(failed))
        self.assertEqual(stat.S_IMODE(os.stat(failed).st_mode), 0o600)
        with open(failed, encoding="utf-8") as fh:
            reason = fh.read()
        self.assertIn("kubeadm-join", reason)
        self.assertIn("exit 3", reason)
        self.assertNotIn("abcdef.0123456789abcdef", reason)
        self.assertFalse(os.path.exists(os.path.join(self.report, "biome-report.done")))

    def test_failure_clears_stale_done_marker(self):
        """A failed run removes a done marker left by an earlier run, so the host never reads a stale success."""
        os.makedirs(self.report)
        with open(os.path.join(self.report, "biome-report.done"), "w", encoding="utf-8") as fh:
            fh.write("old\n")
        self.run_exit(1)
        self.assertFalse(os.path.exists(os.path.join(self.report, "biome-report.done")))
        self.assertTrue(os.path.isfile(os.path.join(self.report, "biome-report.failed")))

    def test_trap_wired_before_params_load(self):
        """The EXIT trap is installed before the params file is sourced, so early failures still report."""
        self.assertLess(self.text.index("trap cleanup_join_material EXIT"), self.text.index('[[ -f "$PARAMS_FILE" ]]'))


if __name__ == "__main__":
    unittest.main()
