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

## Alpha (dal2alpha context)

```bash
make docker-build            # build images locally, tagged ghcr.io/penguintechinc/gough/<svc>:<git-sha8>
make deploy-alpha            # docker push to ghcr.io, helm upgrade --install gough -n gough
make deploy-alpha-clean      # same, after deleting namespace gough
```

`scripts/deploy-alpha.sh` targets kube context `dal2alpha` (override: `GOUGH_KUBE_CONTEXT`), is non-interactive, and attaches the `ghcr-pull` imagePullSecret when that Secret exists. The ghcr packages are public, so the pull secret is optional: to create it once per namespace from a read:packages token, run `GHCR_PULL_USER=<user> GHCR_PULL_TOKEN_FILE=<file> make deploy-alpha` (set both or neither; token read from file, piped via stdin, never in argv). The ES256 OIDC keystore, DB/Redis and admin credentials are generated in-chart and kept across upgrades.

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
