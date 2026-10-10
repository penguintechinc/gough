"""Tests for the Sprint-4 biome cosign signing + verification pipeline.

Covers ``app.api.biomes`` behind ``FLAG_BIOME_SIGNING`` (default OFF):

- Flag OFF: unchanged Sprint-2 ``signing_pending`` placeholder (regression
  guard -- the real pipeline must not change default-OFF behaviour).
- Flag ON, ``POST /{id}/sign``: real cosign sign + post-sign verify + syft
  SBOM, persisted as ``signed``/``signature_verified=True``; any pipeline
  step failing rejects fail-closed (``verification_failed``), never marks
  a biome signed on a partial run.
- Flag ON, ``POST /{id}/upgrade`` (deploy): re-verifies the live cosign
  signature before allowing deployment; an unsigned, never-verified, or
  tamper-detected (live verify fails) biome is rejected fail-closed and
  never reaches orchestration.
- ``gough_biomes_signature_verification_failed_total`` increments on every
  verification failure (sign-time and deploy-time).

cosign/syft/the registry are mocked at the single subprocess boundary
(``biomes_mod._run_subprocess``); object storage is mocked at
``app.services.storage.get_storage_service``. Everything else (argv
construction, JSON digest parsing, DB persistence, the fail-closed gate
logic) runs for real.
"""

from __future__ import annotations

import json

import pytest

import app.api.biomes as biomes_mod
from app import metrics as app_metrics

pytestmark = pytest.mark.asyncio

_DIGEST = "sha256:" + "a" * 64
_SBOM_DOC = json.dumps({"spdxVersion": "SPDX-2.3", "artifacts": []})


@pytest.fixture()
def app_client(app_with_auth):
    """Quart test client with biomes blueprint + auth stubbed."""
    return app_with_auth.test_client()


@pytest.fixture()
def sample_eggs(dal_with_eggs):
    """Same 5-biome catalog as ``tests/api/test_biomes.py`` (module-local; not
    shared via conftest). Index 0 = k8s-primary (``k8s``, no registry_url by
    default), index 4 = app-workload (``user_workload`` -- no cluster.admin
    scope or approval_token required for upgrade).
    """
    biomes = []
    specs = [
        {"name": "k8s-primary", "biome_kind": "k8s", "phase": "phase2_initial", "workload_type": "lxc"},
        {"name": "nest-agent", "biome_kind": "storage", "phase": "post_deploy", "workload_type": "lxc"},
        {"name": "prometheus", "biome_kind": "monitoring", "phase": "post_deploy", "workload_type": "lxc"},
        {"name": "vault-leader", "biome_kind": "infrastructure", "phase": "post_deploy", "workload_type": "vm"},
        {"name": "app-workload", "biome_kind": "user_workload", "phase": "post_deploy", "workload_type": "lxc"},
    ]

    for i, spec in enumerate(specs):
        egg_id = dal_with_eggs.biomes.insert(
            tenant_id="__default__",
            name=spec["name"],
            display_name=spec["name"],
            description=f"Test biome {i}",
            biome_type="lxd_container",
            version="1.0.0",
            biome_kind=spec["biome_kind"],
            phase=spec["phase"],
            workload_type=spec["workload_type"],
            lock_to_host=False,
            is_active=True,
            is_default=False,
        )
        dal_with_eggs.commit()
        biomes.append((int(egg_id), spec["name"]))

    return biomes


@pytest.fixture()
def _flag_on(monkeypatch):
    """Force ``FLAG_BIOME_SIGNING`` on for the duration of the test."""

    async def _enabled(*_a, **_k):
        return True

    monkeypatch.setattr(biomes_mod, "feature_enabled", _enabled)


@pytest.fixture()
def _flag_off(monkeypatch):
    """Force ``FLAG_BIOME_SIGNING`` off explicitly (same as the real default)."""

    async def _disabled(*_a, **_k):
        return False

    monkeypatch.setattr(biomes_mod, "feature_enabled", _disabled)


@pytest.fixture()
def _fake_storage(monkeypatch):
    """Stub the real S3-compatible storage client at its module boundary."""

    class _FakeConfig:
        endpoint_url = "https://minio.gough.test"
        bucket_name = "gough-sboms"

    class _FakeStorageService:
        config = _FakeConfig()

        async def upload_file(self, file_path, object_key, **_kwargs):
            return {"bucket": self.config.bucket_name, "key": object_key}

    async def _fake_get_storage_service(*_a, **_k):
        return _FakeStorageService()

    import app.services.storage as storage_mod

    monkeypatch.setattr(storage_mod, "get_storage_service", _fake_get_storage_service)
    return _fake_get_storage_service


def _cosign_subprocess_double(monkeypatch, *, fail_sign=False, fail_verify=False, digest=_DIGEST):
    """Install a fake cosign/syft subprocess boundary and return its call log."""
    calls: list[list[str]] = []

    async def _fake_run_subprocess(argv, *, timeout):
        calls.append(list(argv))
        binary = argv[0]
        if binary == biomes_mod._COSIGN_BIN and "sign" in argv:
            if fail_sign:
                raise biomes_mod.CosignError("cosign sign exited 1: simulated signing failure")
            return "", ""
        if binary == biomes_mod._COSIGN_BIN and "verify" in argv:
            if fail_verify:
                raise biomes_mod.CosignError(
                    "cosign verify exited 1: no matching signatures (tamper/unsigned)"
                )
            payload = json.dumps([{"critical": {"image": {"docker-manifest-digest": digest}}}])
            return payload, ""
        if binary == biomes_mod._SYFT_BIN:
            return _SBOM_DOC, ""
        raise AssertionError(f"unexpected subprocess invocation: {argv}")

    monkeypatch.setattr(biomes_mod, "_run_subprocess", _fake_run_subprocess)
    return calls


def _metric_value(environment: str = "alpha") -> float:
    return app_metrics.biome_signature_verification_failed.labels(environment=environment)._value.get()


class TestSignBiomeFlagOff:
    """Flag-OFF regression guard — the pipeline must not change default behaviour."""

    async def test_sign_unchanged_when_flag_off(self, app_client, sample_eggs, monkeypatch, _flag_off):
        calls = _cosign_subprocess_double(monkeypatch)
        biome_id = sample_eggs[0][0]
        body = {"key_id": "my-signing-key", "reason": "flag-off regression"}

        response = await app_client.post(f"/api/v1/biomes/{biome_id}/sign", json=body)

        assert response.status_code == 202
        data = await response.get_json()
        assert data["data"]["status"] == "signing_pending"
        assert calls == []  # cosign/syft never invoked while the flag is off


class TestSignBiomeFlagOn:
    """Flag-ON: the real cosign + syft pipeline."""

    async def test_sign_success_persists_signed_and_verified(
        self, app_client, sample_eggs, dal_with_eggs, monkeypatch, _flag_on, _fake_storage
    ):
        calls = _cosign_subprocess_double(monkeypatch)
        biome_id = sample_eggs[0][0]
        dal_with_eggs(dal_with_eggs.biomes.id == biome_id).update(
            registry_url="ghcr.io/penguintechinc/gough/k8s-primary:v1.0.0"
        )
        dal_with_eggs.commit()

        body = {"key_id": "biome-signing-key", "reason": "release v1.0.0"}
        response = await app_client.post(f"/api/v1/biomes/{biome_id}/sign", json=body)

        assert response.status_code == 200
        data = await response.get_json()
        assert data["data"]["status"] == "signed"
        assert data["data"]["image_digest"] == _DIGEST
        assert data["data"]["sbom_url"].endswith(".spdx.json")

        row = dal_with_eggs(dal_with_eggs.biomes.id == biome_id).select().first()
        assert row.signing_status == "signed"
        assert row.signature_verified is True
        assert row.image_digest == _DIGEST
        assert row.sbom_url

        # sign -> verify -> syft, never through a shell, argv lists only
        assert [c[0] for c in calls] == [
            biomes_mod._COSIGN_BIN,
            biomes_mod._COSIGN_BIN,
            biomes_mod._SYFT_BIN,
        ]
        assert "--key" in calls[0] and "hashivault://biome-signing-key" in calls[0]

    async def test_sign_failure_rejects_fail_closed_and_increments_metric(
        self, app_client, sample_eggs, dal_with_eggs, monkeypatch, _flag_on, _fake_storage
    ):
        _cosign_subprocess_double(monkeypatch, fail_sign=True)
        biome_id = sample_eggs[0][0]
        dal_with_eggs(dal_with_eggs.biomes.id == biome_id).update(
            registry_url="ghcr.io/penguintechinc/gough/k8s-primary:v1.0.0"
        )
        dal_with_eggs.commit()

        before = _metric_value()
        body = {"key_id": "biome-signing-key", "reason": "release v1.0.0"}
        response = await app_client.post(f"/api/v1/biomes/{biome_id}/sign", json=body)

        assert response.status_code == 409
        row = dal_with_eggs(dal_with_eggs.biomes.id == biome_id).select().first()
        assert row.signing_status == "verification_failed"
        assert row.signature_verified is False
        assert _metric_value() == before + 1

    async def test_sign_without_registry_url_rejected_before_any_cosign_call(
        self, app_client, sample_eggs, monkeypatch, _flag_on, _fake_storage
    ):
        calls = _cosign_subprocess_double(monkeypatch)
        biome_id = sample_eggs[0][0]  # no registry_url set by the fixture

        body = {"key_id": "biome-signing-key", "reason": "no registry yet"}
        response = await app_client.post(f"/api/v1/biomes/{biome_id}/sign", json=body)

        assert response.status_code == 409
        assert calls == []


class TestUpgradeDeployVerificationGate:
    """FLAG_BIOME_SIGNING gates ``POST /{id}/upgrade`` (the deploy trigger)."""

    async def test_deploy_blocked_for_unsigned_biome_flag_on(
        self, app_client, sample_eggs, monkeypatch, _flag_on
    ):
        calls = _cosign_subprocess_double(monkeypatch)
        biome_id = sample_eggs[4][0]  # user_workload -- no cluster.admin/approval needed

        before = _metric_value()
        body = {"target_version": "1.1.0", "rollout_plan": "auto"}
        response = await app_client.post(f"/api/v1/biomes/{biome_id}/upgrade", json=body)

        assert response.status_code == 409
        data = await response.get_json()
        assert "signature verification" in data["error"]["message"]
        assert calls == []  # unsigned biome never reaches a live cosign call
        assert _metric_value() == before + 1

    async def test_deploy_blocked_when_tampered_signature_fails_live_verify(
        self, app_client, sample_eggs, dal_with_eggs, monkeypatch, _flag_on
    ):
        _cosign_subprocess_double(monkeypatch, fail_verify=True)
        biome_id = sample_eggs[4][0]
        dal_with_eggs(dal_with_eggs.biomes.id == biome_id).update(
            registry_url="ghcr.io/penguintechinc/gough/app-workload:v1.0.0",
            signing_key_id="biome-signing-key",
            signing_status="signed",
            signature_verified=True,
            image_digest=_DIGEST,
        )
        dal_with_eggs.commit()

        body = {"target_version": "1.1.0", "rollout_plan": "auto"}
        response = await app_client.post(f"/api/v1/biomes/{biome_id}/upgrade", json=body)

        assert response.status_code == 409
        row = dal_with_eggs(dal_with_eggs.biomes.id == biome_id).select().first()
        # Fail-closed: a previously-signed biome that fails *live* re-verification
        # (tamper/registry re-point) is demoted, never left looking trustworthy.
        assert row.signature_verified is False
        assert row.signing_status == "verification_failed"

    async def test_deploy_allowed_for_signed_and_verified_biome(
        self, app_client, sample_eggs, dal_with_eggs, monkeypatch, _flag_on
    ):
        calls = _cosign_subprocess_double(monkeypatch)
        biome_id = sample_eggs[4][0]
        dal_with_eggs(dal_with_eggs.biomes.id == biome_id).update(
            registry_url="ghcr.io/penguintechinc/gough/app-workload:v1.0.0",
            signing_key_id="biome-signing-key",
            signing_status="signed",
            signature_verified=True,
            image_digest=_DIGEST,
        )
        dal_with_eggs.commit()

        body = {"target_version": "1.1.0", "rollout_plan": "auto"}
        response = await app_client.post(f"/api/v1/biomes/{biome_id}/upgrade", json=body)

        assert response.status_code == 202
        data = await response.get_json()
        assert data["data"]["upgrade_run_id"]
        # Exactly one live re-verify call, cosign verify only (no re-sign on deploy)
        assert len(calls) == 1
        assert "verify" in calls[0]

    async def test_deploy_gate_skipped_when_flag_off(
        self, app_client, sample_eggs, monkeypatch, _flag_off
    ):
        """Flag OFF: an unsigned biome deploys exactly as before this feature."""
        calls = _cosign_subprocess_double(monkeypatch)
        biome_id = sample_eggs[4][0]  # unsigned, no registry_url

        body = {"target_version": "1.1.0", "rollout_plan": "auto"}
        response = await app_client.post(f"/api/v1/biomes/{biome_id}/upgrade", json=body)

        assert response.status_code == 202
        assert calls == []


class TestCosignHelpers:
    """Unit coverage for the small, pure helper functions."""

    def test_key_ref_wraps_bare_key_id_in_vault_scheme(self):
        assert biomes_mod._cosign_key_ref("my-key") == "hashivault://my-key"

    def test_key_ref_passes_through_explicit_scheme(self):
        assert biomes_mod._cosign_key_ref("awskms://alias/my-key") == "awskms://alias/my-key"

    def test_parse_cosign_digest_valid(self):
        stdout = json.dumps([{"critical": {"image": {"docker-manifest-digest": _DIGEST}}}])
        assert biomes_mod._parse_cosign_digest(stdout, "ref") == _DIGEST

    @pytest.mark.parametrize(
        "stdout",
        [
            "not json",
            "{}",
            json.dumps([{"critical": {"image": {}}}]),
            json.dumps([{"critical": {"image": {"docker-manifest-digest": "not-a-digest"}}}]),
        ],
    )
    def test_parse_cosign_digest_rejects_malformed_output(self, stdout):
        with pytest.raises(biomes_mod.CosignError):
            biomes_mod._parse_cosign_digest(stdout, "ref")


class TestArgvFlagInjectionRejected:
    """regression: argv flag-smuggling (background commit security review).

    A biome ``registry_url`` is operator-controlled and flows as a positional
    into cosign/syft argv. argv lists stop shell injection but not argument
    injection: a value beginning with ``-`` is parsed as a CLI flag. The
    pipeline must reject such a reference fail-closed BEFORE any subprocess.
    """

    @pytest.mark.parametrize(
        "bad_ref",
        [
            "--output=/tmp/evil",
            "-o",
            "--yes",
            "registry.io/x y:1",  # internal whitespace
            "",
        ],
    )
    def test_validate_image_ref_rejects_flaglike(self, bad_ref):
        with pytest.raises(biomes_mod.CosignError):
            biomes_mod._validate_image_ref(bad_ref)

    def test_validate_image_ref_accepts_normal(self):
        ref = "registry.example.io/biomes/web@sha256:" + "a" * 64
        assert biomes_mod._validate_image_ref(ref) == ref

    @pytest.mark.asyncio
    async def test_cosign_verify_flag_ref_never_spawns_subprocess(self, monkeypatch):
        async def _boom(*a, **k):
            raise AssertionError("subprocess spawned for a flag-like image ref")

        monkeypatch.setattr(biomes_mod, "_run_subprocess", _boom)
        with pytest.raises(biomes_mod.CosignError):
            await biomes_mod._cosign_verify("--output=/tmp/evil", key_id="k1")
