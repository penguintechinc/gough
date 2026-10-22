# api-manager Helm Chart

## ES256 OIDC Keystore (penguin-aaa)

api-manager is its own first-party OIDC issuer: it mints and validates ES256
access/id tokens locally via a penguin-aaa `FileKeyStore`, no external
JWKS/discovery endpoint. **All replicas share the same key file** — since
each replica both issues and validates tokens, a per-pod generated key would
make tokens minted by one replica unverifiable by another. The key is
therefore mounted from a single Kubernetes Secret (`api-manager-oidc-keystore`
by default, see `values.yaml` → `keystoreSecret.secretName`), never generated
in-cluster and never committed to this repo.

### One-time ES256 keygen

```bash
# 1. Generate an EC (P-256) private key
openssl ecparam -name prime256v1 -genkey -noout -out private.pem

# 2. Wrap it into the penguin-aaa FileKeyStore JSON format
kid="gough-prod-key-1"   # bump the suffix on every rotation
pem_json=$(python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' < private.pem)
cat > keys.json <<EOF
{"keys":[{"kid":"${kid}","pem":${pem_json}}]}
EOF

# 3. Load into the cluster — pick ONE:

# 3a. Vault (preferred; feeds the ExternalSecret in templates/externalsecret.yaml)
vault kv put secret/gough/api-manager/oidc-keystore oidc_keys.json=@keys.json

# 3b. Sealed Secrets
kubectl create secret generic api-manager-oidc-keystore \
  --from-file=oidc_keys.json=keys.json --dry-run=client -o yaml \
  | kubeseal --format yaml > sealed-oidc-keystore.yaml
kubectl apply -f sealed-oidc-keystore.yaml

# 3c. Manual (bootstrap only — prefer 3a/3b for anything long-lived)
kubectl create secret generic api-manager-oidc-keystore \
  --from-file=oidc_keys.json=keys.json -n gough

# 4. Shred the local plaintext copies
shred -u private.pem keys.json
```

### Enabling the ExternalSecret stub

`templates/externalsecret.yaml` is disabled by default (`externalSecret.enabled:
false`). Once External Secrets Operator and a Vault-backed
`SecretStore`/`ClusterSecretStore` exist in the target cluster, set in the
relevant per-environment values file:

```yaml
externalSecret:
  enabled: true
  secretStoreRef:
    name: <your-clustersecretstore-name>
    kind: ClusterSecretStore
```

### Rotation

Rotating the key means updating the Secret content (new `kid` + `pem` entry
in `keys.json`, written via Vault/kubeseal/kubectl as above) and rolling all
api-manager replicas so they pick up the new file — the chart does not
automate this; `checksum/config` in `templates/deployment.yaml` only tracks
the ConfigMap, not this Secret, so a manual rollout
(`kubectl rollout restart deployment/api-manager -n gough`) is required after
updating the Secret.

### Values files

`values-gamma.yaml` and `values-production.yaml` do not exist yet for this
chart (no chart in this repo has them yet — gamma/production Helm
environments haven't been stood up). `OIDC_ISSUER`/`OIDC_AUDIENCE` are wired
in `values.yaml` (generic placeholder), `values-alpha.yaml`, and
`values-beta.yaml`. When gamma/production values files are created, set
`OIDC_ISSUER` to `https://gough-gamma.penguintech.cloud` and
`https://gough.app` respectively (see `env` list pattern in
`values-alpha.yaml`/`values-beta.yaml` — these files fully replace, not
merge, the base `env` list, so `OIDC_ISSUER`/`OIDC_AUDIENCE`/
`GOUGH_KEY_STORE_PATH` must be repeated literally in each new file).
