# Secrets in GitOps mode: Vault and External Secrets

In `deploy.ps1 -GitOps` the stand's three Kubernetes Secrets are not created by
the script any more. Their values live in **Vault**, and **External Secrets
Operator** (ESO) copies them into the cluster. Git holds only *where each Secret
comes from*, never a value.

Nothing needs to be installed on your PC: Vault is a pod inside the cluster, and
the script talks to it through `kubectl exec`.

## The three pieces

| Piece | What it is | Where it runs |
| ----- | ---------- | ------------- |
| **Vault** (HashiCorp) | a secret store: values are kept in it, access is by policy, every read is authenticated | a pod in namespace `vault`, **dev mode** |
| **External Secrets Operator** | a controller that watches `ExternalSecret` objects, reads the named values from Vault and creates ordinary Kubernetes Secrets | three pods in namespace `external-secrets` |
| **`ExternalSecret` objects** (this directory) | "build the Secret `grafana-admin` from `secret/sock-shop/grafana` in Vault" - a recipe, no values | in git, synced by Argo CD |

The applications do not know any of this. Grafana, Alertmanager and the triage bot
mount the Secrets `grafana-admin`, `alertmanager-secrets` and `aiops-llm` exactly as
before.

## How a value travels

    deploy/secrets.local.ps1                  (your machine, git-ignored)
            |
            |  deploy/vault-seed.ps1          writes over stdin, never as arguments
            v
    Vault   secret/sock-shop/{grafana,alerting,aiops}
            ^
            |  every minute: login with the pod's own ServiceAccount token (below),
            |  read the values
    External Secrets Operator   <--  ExternalSecret objects (git, via Argo CD)
            |
            v
    Kubernetes Secrets  grafana-admin / alertmanager-secrets / aiops-llm   (monitoring)
            |
            v
    Grafana, the import Job, Alertmanager, the triage bot

Rotating a value is one command and no redeploy: change it in Vault (or edit
`secrets.local.ps1` and run `deploy\vault-seed.ps1`), and within about two minutes
the Secret is updated, the file in the pod follows, and the Alertmanager
config-reloader sidecar reloads it (`FIXES.md`, section 31).

## How ESO proves who it is: no token is stored anywhere

The usual weak point of a secret store is the credential used to reach it. Here
there is none to keep:

1. the ESO pod has a Kubernetes **ServiceAccount token** (issued and rotated by
   the cluster);
2. it presents that token to Vault's **Kubernetes auth method**;
3. Vault asks the Kubernetes API "is this token real, and whose is it?"
   (TokenReview) and checks the answer against a **role**: only the ServiceAccount
   `external-secrets` of the namespace `external-secrets` may use it;
4. Vault answers with a short-lived Vault token that carries one **policy**:
   read-only access to `secret/sock-shop/*`.

`deploy/vault-seed.ps1` creates that policy and role. ESO can therefore read these
three paths and nothing else, and cannot write at all.

## Dev mode: what it means, honestly

Vault here runs with `server.dev.enabled=true`: **one pod, already unsealed, data
in memory, a fixed root token (`root`)**.

* Everything in it is **lost when the pod restarts** (Docker Desktop restarted,
  the node rebooted). The Secrets already in the cluster stay - the
  `ExternalSecret` objects use `deletionPolicy: Retain` - so a running stand keeps
  working; but nothing refreshes them, and a new pod would wait for them. Run
  `.\deploy\vault-seed.ps1` again and it is back (it is idempotent, and reuses the
  Grafana password already stored).
* The root token is public (it is in the Helm values in git). That is acceptable
  only because the Vault is throw-away and reachable only inside the cluster.
* A real Vault is a different shape: highly available Raft storage on persistent
  volumes, auto-unseal through a cloud KMS, audit logging, and no static root
  token. This setup shows the **mechanism** (store, policy, role, operator,
  rotation); it is not a hardened deployment.

## Looking at it

    kubectl port-forward -n vault svc/vault 8200:8200      # http://localhost:8200
    # sign in with the token: root; the values are under secret/sock-shop

    kubectl -n monitoring get externalsecret               # STATUS SecretSynced, READY True
    kubectl get clustersecretstore vault                   # READY True

An `ExternalSecret` that says `SecretSyncedError` is almost always one of: Vault
was restarted and is empty (run the seed script), or the role does not exist yet
(same cure).

## Files

| File | What it says |
| ---- | ------------ |
| `00-cluster-secret-store.yaml` | where Vault is and how to log in (Kubernetes auth, role `external-secrets`) |
| `10-grafana-admin.yaml` | `grafana-admin` <- `secret/sock-shop/grafana` |
| `11-alertmanager-secrets.yaml` | `alertmanager-secrets` <- `secret/sock-shop/alerting` |
| `12-aiops-llm.yaml` | `aiops-llm` <- `secret/sock-shop/aiops` (the key is optional: empty means "no model") |
