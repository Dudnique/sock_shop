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
| **Vault** (HashiCorp) | a secret store: values are kept in it, access is by policy, every read is authenticated | a pod in namespace `vault`, data on a volume (Raft), sealed after every start |
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

## Storage, sealing and the root token: what it means

Three properties separate this Vault from a throw-away "dev mode" one (which this
stand used until section 34 of `FIXES.md`):

* **The data is on disk.** Vault stores it itself (the built-in storage is called
  Raft) on a 1 Gi volume, so a pod restart does not lose it. Nothing has to be
  written again.
* **Vault starts sealed.** Sealed means locked: it holds the data but cannot read
  it and gives nothing out until it is unsealed with a key. The key is generated
  once, at the first start, by `deploy/vault-seed.ps1` and saved with the root
  token in `deploy/vault-init.local.json` - **git-ignored, next to
  `secrets.local.ps1`; whoever holds the file owns the Vault, and losing it loses
  the data**. After a restart (Docker Desktop restarted, the node rebooted) one
  command unlocks it and touches nothing else:

      .\deploy\vault-seed.ps1 -UnsealOnly

  While it is sealed, External Secrets cannot read it: the Secrets already in the
  cluster keep working (`deletionPolicy: Retain`), and `ExternalSecretNotReady`
  fires after 5 minutes. Running `vault-seed.ps1` without the flag also unseals,
  and rewrites the values from `secrets.local.ps1`.
* **There is no public root token.** The root token is generated at the first
  start and exists only in that local file. The script passes it to the pod over
  stdin, not on a command line.

If the cluster is rebuilt, the volume is new and the key file is stale: the script
notices (Vault is uninitialised but the file exists), moves the old file aside as
`vault-init.local.json.stale-<time>` and initialises afresh. If the file is lost
but the volume is not, the data cannot be opened - the script says so and what to
delete if the data does not matter.

What is still a laptop shape: **one pod, one key**. A production Vault runs three
or five pods (Raft replicates between them), splits the key in several shares held
by different people, and in AWS is unsealed by KMS on its own (auto-unseal), so
nobody unseals by hand at all. That is the AWS step; the mechanism shown here
(store, policy, role, operator, rotation, persistence) is the same.

## Looking at it

    kubectl port-forward -n vault svc/vault 8200:8200      # http://localhost:8200
    # sign in with the root_token from deploy\vault-init.local.json; values: secret/sock-shop

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
