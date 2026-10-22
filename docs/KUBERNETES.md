# Kubernetes Deployment (Helm Only)

Helm v4 is the only supported deployment method. Kustomize and raw manifest directories are not used.

## Charts

```
k8s/helm/
├── gough/          # umbrella: hub deploy (api-manager, webui, worker-ipxe, + in-chart Postgres/Redis non-prod)
├── api-manager/    # subchart
├── webui/          # subchart
├── worker-ipxe/    # subchart
├── access-agent/   # hypervisor-side agent - NOT part of the hub umbrella
└── discovery-agent/# hypervisor-side agent - NOT part of the hub umbrella
```

Values per environment: `alpha.yml`, `beta.yml`, `gamma.yml`, `production.yml` (`values.yaml` is the base).

## Alpha (local-alpha context)

```bash
make docker-build            # build images locally (tag alpha-local)
make deploy-alpha            # imports images, helm upgrade --install gough -n gough
make deploy-alpha-clean      # same, after deleting namespace gough
```

`scripts/deploy-alpha.sh` always passes `--kube-context local-alpha` and is non-interactive. The ES256 OIDC keystore, DB/Redis and admin credentials are generated in-chart and kept across upgrades.

## Validation

```bash
helm dependency update k8s/helm/gough
helm lint k8s/helm/gough -f k8s/helm/gough/alpha.yml
helm template gough k8s/helm/gough -f k8s/helm/gough/alpha.yml -n gough | kubeconform -strict -ignore-missing-schemas
```

Non-alpha renders require the image tag from CI: `--set global.imageTag=<tag>`.

## Secrets

- Generated on first install (`lookup` + `randAlphaNum`) and kept across upgrades: DB, Valkey, admin, secret-key, worker API key, OIDC ES256 keystore (`genPrivateKey "ecdsa"`, `oidcKeystore.generate`).
- Object storage (worker-ipxe): `global.objectStorage.endpoint` required while worker-ipxe is enabled; credentials via `accessKeySecretRef` (preferred) or inline dev values.
- Supplied at install time, never committed: `secrets.licenseKey`, storage keys, `externalDatabase.password`, `externalRedisUrl`.
