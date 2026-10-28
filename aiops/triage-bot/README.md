# AIOps triage bot (milestone M1 - advisor only)

Alertmanager hands this service one firing alert. The bot collects read-only
evidence from Prometheus and Loki, decides what to do - or refuses to decide -
and puts the result into Telegram as a card with **approve / details / report /
ignore** buttons. In M1 it executes nothing, and that is a property of the artifact rather
than a promise in a document (see [The guardrail](#the-guardrail)).

The image is `python:3.12-slim` plus one package: **no dependencies, no
Kubernetes client, no `kubectl`, no shell**. The standard library is the whole
dependency list, which is also why `unittest` is the entire test runner and CI
needs no `pip install`, no lockfile and no cache.

## What happens to one alert

    Alertmanager --POST /alert--> server.py
                                   |
                                   +- filter    aiops="true", status=firing, not snoozed,
                                   |            not a duplicate (30 min window)
                                   +- context   Prometheus (planned queries) + Loki (logs);
                                   |            whatever was not collected goes into `missing`
                                   +- tier 0    MockAdapter - the ruleset, in code
                                   |            confident answer? -> done, no model call
                                   +- tier 1    the model, ONLY if tier 0 escalated
                                   |            (DeepSeek / OpenAI / Ollama / any
                                   |             OpenAI-compatible gateway)
                                   +- validate  strict JSON, action id from the whitelist,
                                   |            params in range, confidence >= threshold
                                   +- notify    Telegram card + journal + /metrics
                                                nothing is executed

Three properties of that picture are worth stating out loud, because each one is
the answer to a failure this project has already had once:

* **a model is never required for the bot to be useful** - tier 0 is the
  ruleset, and it alone answers the alerts this stand actually fires, from a
  table that a test keeps in sync with the rule file. `MODEL_ADAPTER` selects
  tier 1 (the shipped ConfigMap says `deepseek`); `mock` there means "no model
  at all" - the offline stand and CI;
* **a model is never trusted** - its answer travels the same validator as the
  ruleset's, and a rejected answer becomes an escalation, never an action;
* **a gap is reported, not filled** - what the bot could not see (pod events,
  node conditions, the `describe` output) is listed in `missing` on the card. An
  honest gap costs one line; a guess costs the credibility of every other line.

## The guardrail

M1 is an advisor: it can propose, it cannot act. That is enforced at four
independent levels, so a mistake in any one of them is not enough to break it:

| Level | Fact | Where it is visible |
| ----- | ---- | ------------------- |
| Pod | `automountServiceAccountToken: false` and no `serviceAccountName`, so there is no token to authenticate with | `manifests-aiops/02-triage-bot-dep.yaml` |
| RBAC | no Role, ClusterRole, RoleBinding or ClusterRoleBinding anywhere in this component | greped by the `aiops-tests` CI job, asserted in `tests/test_manifests.py` |
| Image | no `kubectl`, no shell, no Kubernetes client library to import | `Dockerfile`; CI runs `docker run --entrypoint python ... -c "import triage_bot, ..."` |
| Code | the action whitelist and the decision schema live in `triage_bot/model.py`, and `TriagePipeline.execute()` raises `NotImplementedError` | `model.ACTIONS`, `pipeline.execute()` |

The whitelist is in code on purpose. In a ConfigMap it would be editable by
anyone who can edit configuration, which would turn "the model cannot do anything
unapproved" into "the model cannot do anything outside the list somebody chose
last week". `pipeline.execute()` exists only to say so out loud, and its docstring
is the specification of what milestone M2 has to add.

## The decision contract

Both tiers answer in the same shape - one JSON object and nothing else:

```json
{"action_id": "restart_deployment",
 "confidence": 0.85,
 "reason": "1-3 sentences, in Russian",
 "evidence": ["replicas_available[front-end] = 0", "log: connection refused"],
 "params": {"namespace": "sock-shop", "deployment": "front-end"}}
```

| Key | Required | Rule |
| --- | -------- | ---- |
| `action_id` | yes | must be one of the four ids below - the whitelist is `model.ACTIONS` |
| `confidence` | yes | a number in `0..1`; below `CONFIDENCE_THRESHOLD` (0.7) an action is rejected and the incident escalates |
| `reason` | yes | non-empty text, truncated at 600 chars |
| `evidence` | yes | a non-empty list; entries come from the collected context, never from the model's imagination (at most `MAX_EVIDENCE_ITEMS`) |
| `params` | for actions that need them | exactly the expected set: `namespace`, `deployment` are DNS-1123 names, `replicas` is an integer in `1..10`, and `namespace` must be in `ALLOWED_NAMESPACES` |
| `proposal` | no | free text, max 500 chars, **allowed only together with `escalate_human`** |

| `action_id` | Risk | Command M2 would run |
| ----------- | ---- | -------------------- |
| `restart_deployment` | low | `kubectl -n {namespace} rollout restart deployment/{deployment}` |
| `rollback_deployment` | medium | `kubectl -n {namespace} rollout undo deployment/{deployment}` |
| `scale_deployment` | medium | `kubectl -n {namespace} scale deployment/{deployment} --replicas={replicas}` |
| `escalate_human` | none | (no command - the incident goes to a human) |

Every command is rendered **by this code** from validated parameters. The model's
text is never interpolated into anything, and `_NAME_RE` rejects a name before it
could ever reach a shell. Extra keys, a prose answer, markdown fences, an unknown
action, a replica count of 0 - each one is a rejection, and a rejection is an
**escalation with the reason attached**, counted in
`aiops_schema_violations{kind=...}`. A prompt that produces invalid answers is a
defect to be measured, not a mystery to be retried.

`proposal` is the one place where free text is allowed, and only next to
`escalate_human`: it is what the model *would* have done if the whitelist were
wider. It is shown to a human and there is no code path anywhere that turns it
into a command.

## The cascade: tier 0 is not a mock-up

`MockAdapter` is the tier-0 ruleset - decisions written by hand and keyed by the
alert names this stand fires (`manifests-monitoring/05-prometheus-alertrules.yaml`).
It is not a stand-in for a model; it is the first answer, and on a well-behaved
stand it is usually the only one:

* **the keys cannot drift from the rules** - `tests/test_coverage.py` fails in CI
  if a shipped rule that carries `aiops="true"` has no answer in the table, so
  "the bot forgot about a new alert" is caught before a demo;
* **the card is not fictional** - the *decision* is a template, but the *evidence*
  and the *params* come from the live context: real Prometheus values, real Loki
  lines, real alert labels;
* **it is deterministic**, which is what makes `EVAL_MODE` meaningful: the ruleset
  is the baseline every model answer is compared against on the very same alert;
* **`ProbeFailed` and `ProbeSlow` are deliberately below the confidence
  threshold**, so the model tier opens on them - and a test asserts exactly that.

`EVAL_MODE=1` calls the model even when the ruleset answered, keeps both answers
in the journal and on the card, and the ruleset's answer still counts. That is how
"the model is worth its price" becomes numbers (`aiops_incidents_tier{tier=...}`,
tokens, latency, agreement) instead of an opinion.

## What the live runs found

All of them were found by running the bot against the live stand - not by reading
the code, and not by the tests, which were green before and after.

**1. The Loki selector asked for a label that does not exist.** Promtail cuts its
labels out of the log *file path* on the node
(`manifests-logging/04-promtail-configmap.yaml`), so Loki knows `namespace`,
`pod_name` and `container_name` - and has no `pod` and no `deployment` label at
all. The first version reused the Prometheus names, the selector matched nothing,
and the card said "loki: no log lines in the window" about a front-end that was
logging continuously. Fixed in `context.collect_context`; the regression test
(`tests/test_context.py`) fails if the selector ever goes back to `pod=`.
Verified live: `GET /loki/api/v1/labels` on the stand answers exactly
`container_name, filename, job, namespace, pod_name, stream`.

**2. Tier 0 trusted the alert.** A synthetic `DeploymentReplicasUnavailable` POST
against a perfectly healthy front-end produced a confident `restart_deployment`.
The alert was the only input the ruleset checked. Now every ruleset answer whose
alert is listed in `MockAdapter.PREMISES` is re-checked against the very series it
is meant to be computed from (`replicas_available` vs `replicas_desired`), and a
contradiction turns the answer into an escalation that carries **both numbers**:

    Алерт DeploymentReplicasUnavailable не подтверждается метриками
    (replicas_available=1 >= replicas_desired=1): того, на что указывает алерт,
    сейчас не видно, автоматический шаг ничего не исправит - решение за человеком.

The asymmetry is deliberate and is the whole policy: only a **contradiction**
stops the bot. A **missing** metric is not a conflict - silence must not block a
safe step, or the bot would stop acting on every stand where one exporter is
broken.

**3. A health field that lied.** `/healthz` reported
`model_tier_reason: "the provider has no base_url"` on a stand with
`MODEL_ADAPTER=mock`, where nothing is missing at all. `model_tier_reason` now
returns an empty string for `mock`, because `mock` *is* the tier-0 ruleset: a
misleading health field costs more than the three lines it takes to remove it.

**4. "No log lines" about a deployment that has no pods.** In the M1 demo
`front-end` was scaled to `0` and the card listed
`loki: no log lines in the window` - the very sentence a broken selector produced
in bug 1, so it pointed the reader at Loki instead of at `desired=0`. The context
already held `replicas_desired[front-end]=0`; `context._is_scaled_to_zero()` now
reads it, and the card says `loki: no pods - the deployment is scaled to 0
(desired=0), so there is nothing to log`. An unknown number keeps the old wording,
because silence must not be turned into a diagnosis either.

**5. Pod alerts never reached a Deployment.** `PodCrashLooping` and `PodRestartFlapping`
carry `pod` and `container` and no `deployment`, so tier 0 stopped at "no deployment
label" and handed every such alert to a human - the bot could not propose anything for
the most common kind of incident. (An earlier version of these notes said the opposite:
that tier 0 proposed a restart for a healthy pod. It did, but only for a test alert to
which a `deployment` label had been added by hand; the shipped rule never sends one.)
`context.resolve_deployment()` now asks kube-state-metrics for the owner chain,
`kube_pod_owner` -> ReplicaSet -> `kube_replicaset_owner` -> Deployment, instead of
guessing it from the pod name, and the card states it as its first fact
(`deployment_of_pod[...] = catalogue`). A pod name from an alert is checked against
the DNS-1123 pattern before it goes into a PromQL string.

**6. Once the Deployment is known, the alert is re-checked** (`MockAdapter.POD_PREMISES`):

| Alert | The premise it must still satisfy | Query |
| ----- | --------------------------------- | ----- |
| `PodCrashLooping` | the pod was in `CrashLoopBackOff` in the last 5 minutes | `max_over_time(kube_pod_container_status_waiting_reason[5m])` |
| `PodRestartFlapping` | more than 3 restarts in 15 minutes (the rule's own number; `tests/test_coverage.py` fails if they differ) | `increase(kube_pod_container_status_restarts_total[15m])` |

The check needed one change in the context: it now records which queries **answered
with no series** (`context["empty"]`) separately from the ones that **failed**
(`context["missing"]`). An empty answer is evidence - "this pod is not waiting" - and
refuses the action; a failed query is no evidence and, as before, does not block it.
The first version asked "is it waiting right now?" and was wrong on a live 2-minute-old
crash-looping pod: a container leaves the waiting state for a moment on every attempt,
and the instant query landed in that moment. The 5-minute look-back fixed it.

Verified on the live stand with a Deployment that really crash-loops: `PodCrashLooping`
and `PodRestartFlapping` on that pod -> `restart_deployment` for the resolved
Deployment; the same two alerts on a healthy `catalogue` pod -> `escalate_human`
("no container of the pod is in a waiting state" / "pod_restarts_15m = 0, the rule
needs more than 3").

### A live model run: the first real `bad_params`

`run-local.ps1 -Model deepseek -Alertname ProbeFailed -Fresh` against the stand:
tier 0 escalated on its own confidence (0.60 versus the 0.70 threshold - by
design, not by failure), `deepseek-chat` answered in **1.6-1.9 s** for **~900
tokens**, and its answer was `escalate_human` **with** `deployment`, `namespace`
and `replicas` attached. `escalate_human` takes no params, so the validator
threw the answer away, kept the escalation and recorded why:

```
answer rejected by the validator (bad_params):
escalate_human needs exactly no params, got ['deployment', 'namespace', 'replicas']
```

The action was right and its shape was wrong - precisely the case the schema
check exists for. A reviewer reading only `action_id` would have approved a
decision the contract had already rejected. Two runs, the same outcome.

## Files

| File | Purpose |
| ---- | ------- |
| `triage_bot/config.py` | every setting, read from environment variables; secrets only from files |
| `triage_bot/context.py` | read-only Prometheus and Loki clients, and the `missing` list they produce |
| `triage_bot/adapters.py` | `MockAdapter` (the tier-0 ruleset, `RULES`, `PREMISES`) and `OpenAICompatibleAdapter` |
| `triage_bot/model.py` | the contract: decision schema, action whitelist, prompt, validator |
| `triage_bot/pipeline.py` | the cascade, dedupe/snooze, `handle_webhook`, `handle_callback` |
| `triage_bot/telegram.py` | the card, its keyboard, `sendDocument`, the long-poll loop for button presses |
| `triage_bot/report.py` | one incident plus its human decisions as a Markdown report |
| `triage_bot/journal.py` | incidents and human decisions on disk (`/data`, append-only JSONL) |
| `triage_bot/metrics.py` | the `aiops_*` series in the Prometheus text format |
| `triage_bot/server.py` | five HTTP routes on `http.server` - no framework, no WSGI server |
| `tests/` | the offline suite (see [Tests and CI](#tests-and-ci)) |
| `run-local.ps1` | runs the whole pipeline on a workstation against the live stand |
| `Dockerfile` | `python:3.12-slim` plus the package, non-root, no writable root |

## Running it locally (no cluster rebuild, no key, no Docker)

```powershell
cd aiops\triage-bot
.\run-local.ps1                  # tier 0 only: model=mock, Telegram off
.\run-local.ps1 -Fresh           # wipe the local journal first
.\run-local.ps1 -Model deepseek  # the same cascade as in the cluster, with a key
```

The script reaches Loki through `kubectl port-forward` (Prometheus is already
published on host port 9090 by the k3d mapping), starts `python -m triage_bot` on
port **8090**, POSTs a realistic Alertmanager payload and prints three things: the
response, the last incident as a human sees it, and the `aiops_*` metrics.

| Parameter | Default | Why it is what it is |
| --------- | ------- | -------------------- |
| `-Model` | `mock` | `mock`, `deepseek`, `openai`, `ollama` - the identical cascade, only the tier-1 adapter changes |
| `-Alertname` | `DeploymentReplicasUnavailable` | any alert name the ruleset knows; use a fake one to watch it escalate |
| `-Port` | `8090` | **not** 8080: that host port belongs to the front-end Ingress (Traefik), and Windows happily binds a port that is already in use - the POST then lands on Node and answers `Cannot POST /alert` |
| `-Fresh` | off | without it, a second POST inside the 30-minute dedupe window is (correctly) reported as `duplicate`. Run once with `-Fresh` and once without: that is the duplicate filter working, not the bot failing |

`-Model deepseek` reads `DEEPSEEK_API_KEY` from `deploy\secrets.local.ps1`
(git-ignored, the same file the alerting Secret is built from), copies it into a
temp file - because the code reads secrets from **files** (`LLM_API_KEY_FILE`) and
never from a variable - and deletes that file in the `finally` block. The key is
never printed, never passed on a command line and never reaches the journal.

## Deploying it

```powershell
.\deploy.ps1 -WithAIOps              # build + import + secret + apply + wait
.\deploy.ps1 -WithAIOps -SkipBuild   # re-apply only
```

`-WithAIOps` does five things, in this order:

1. `docker build -t triage-bot:0.1.0-m1 aiops/triage-bot` - that tag is the one
   `02-triage-bot-dep.yaml` names, and a test asserts the two cannot drift;
2. `k3d image import triage-bot:0.1.0-m1 -c <cluster>` - the image is published
   nowhere, which is exactly why the Deployment uses
   `imagePullPolicy: IfNotPresent`;
3. creates the `aiops-llm` Secret **idempotently** when `DEEPSEEK_API_KEY` (or
   `deploy\secrets.local.ps1`) provides one, and skips it with a printed note
   otherwise - the mount is `optional: true`, so a keyless stand still runs;
4. `kubectl apply -f deploy\kubernetes\manifests-aiops` (never `examples\`:
   `kubectl apply -f <dir>` is not recursive);
5. waits for the rollout and prints one summary line per step.

Switching the tier-1 provider is a ConfigMap edit, never a code change: set
`MODEL_ADAPTER` in `01-triage-bot-configmap.yaml` and re-apply. The shipped value
is `deepseek`, so on any alert the ruleset is unsure about the next card carries
both answers - the ruleset's and the model's; `openai` and `ollama` are rows in
the same provider table. `MODEL_ADAPTER: mock` means there is no model tier at
all and the pipeline ends at the ruleset: that is the offline stand (no key, no
outbound network) and it is what the unit tests run on.

## Configuration reference

Everything is an environment variable, fed from `01-triage-bot-configmap.yaml`;
there is no config file to parse, which is why the image needs no YAML library.
Secrets are the only thing that is *not* a variable - `llm_api_key()` and
`telegram_token()` read mounted files, because an environment variable is visible
in `kubectl describe pod` and to every child process.

| Variable | Default in code | Set by the ConfigMap | Meaning |
| -------- | --------------- | -------------------- | ------- |
| `LISTEN_HOST` / `LISTEN_PORT` | `0.0.0.0` / `8080` | - | where `ThreadingHTTPServer` listens |
| `MODEL_ADAPTER` | `mock` | `deepseek` | `mock`, `deepseek`, `openai`, `ollama`; an unknown name is shown, never guessed |
| `MODEL_NAME` | provider default | - | overrides the model id (`deepseek-chat`, `gpt-4o-mini`, `llama3.1`) |
| `LLM_BASE_URL` | provider default | - | any OpenAI-compatible gateway, vLLM or LiteLLM proxy included |
| `LLM_API_KEY_FILE` | provider default | - | the mounted key file; an empty file means keyless (Ollama) |
| `LLM_TEMPERATURE` | `0.0` | - | zero: an incident is a fact, not a creative task |
| `LLM_MAX_TOKENS` | `700` | `700` | enough for one JSON decision, small enough to be cheap |
| `LLM_TIMEOUT_SECONDS` | `30` | `30` | a slow provider must not hold the webhook open forever |
| `CASCADE_ENABLED` | `1` | `1` | ask the model only when the ruleset itself escalates |
| `EVAL_MODE` | `0` | `0` | call the model even when the ruleset answered, and keep both answers |
| `LLM_FALLBACK_TO_MOCK` | `1` | - | no key: fall back to the ruleset and **say so** in the card |
| `CONFIDENCE_THRESHOLD` | `0.7` | `0.7` | below this an action is rejected and escalates |
| `REQUIRE_LABEL` | `aiops` | `aiops` | only alerts carrying this label are triaged at all |
| `ALLOWED_NAMESPACES` | `sock-shop` | `sock-shop` | the only namespaces the bot may *name* |
| `DEDUPE_WINDOW_SECONDS` | `1800` | `1800` | one fingerprint inside this window is one incident |
| `LOG_WINDOW_SECONDS` | `900` | `900` | how far back Loki is asked for lines |
| `MAX_LOG_LINES` | `40` | `40` | log lines per incident - a card, not a log dump |
| `MAX_EVIDENCE_ITEMS` | `6` | - | evidence beyond this is trimmed |
| `HTTP_TIMEOUT_SECONDS` | `10` | `30` | 30 for the cold-TSDB first query (see the ConfigMap comment) |
| `PROMETHEUS_URL`, `LOKI_URL`, `ALERTMANAGER_URL` | in-cluster names | same | the read-only sources |
| `TELEGRAM_TOKEN_FILE` | `/etc/aiops/secrets/...` | `/etc/aiops/telegram/telegram_bot_token` | the token is a file, never a variable |
| `TELEGRAM_CHAT_ID` | (empty) | the stand's chat | not a secret - it is already visible in the Alertmanager ConfigMap |
| `TELEGRAM_MAX_PER_HOUR` | `20` | `20` | after that: `aiops_telegram_suppressed`, and the bot still triages |
| `JOURNAL_DIR` | `/data` | `/data` | the only writable path (read-only root filesystem) |
| `LOG_LEVEL` | `INFO` | `INFO` | |

`tests/test_manifests.py` reads the ConfigMap keys out of the YAML and the variable
names out of `config.py::from_env`, and fails on any name present in one and not
the other. A typo in a ConfigMap key is otherwise invisible: the bot silently keeps
its default and nobody notices until a demo.

## HTTP surface

| Route | Answers |
| ----- | ------- |
| `POST /alert` | The Alertmanager webhook: `{"received": N, "triaged": [...], "skipped": [...]}`. A bad body is `400` (Alertmanager retries, loudly); a *triage* failure is `200` with the error in the body - a model that is down must not become a retry storm against the bot |
| `GET /healthz` | `{"status": "ok", "version": ..., "settings": {...}}` - the whole configuration, secret-free, so "which adapter answered" needs no card |
| `GET /metrics` | Prometheus text format |
| `GET /incidents?limit=N` | The journal, newest first (default 10, max 200) - the same data a human sees in chat, which is what makes the stand debuggable without Telegram |
| `POST /decision` | A synthetic button press (`{"incident_id": ..., "action": "approve"}`); it calls the *same* pipeline method as the Telegram callback, so the offline demo cannot drift from the real one |

## The Telegram card

One incident, one message: the cause first, the facts it rests on second, and only
then the step - a whitelisted command, a free-text proposal the bot refuses to
execute, or the alert's own runbook when there is no command at all. The bot's
bookkeeping (adapter, tokens) is one footer line; the tier-by-tier comparison
stays in the journal, where it is a measurement rather than news.

Buttons come from the whitelist, not from the text. `approve` exists only for an
executable decision - an escalation offers the follow-ups and `ignore`.
`💡 подробнее` makes a **second, plain-text model call** (no JSON schema, no
validator, no execution): a few hypotheses, what would confirm them, what would
rule them out. `📄 отчёт` sends the incident as a Markdown document
(`triage_bot/report.py`) that includes the cascade the card hides and every human
press with its timestamp - the artifact that outlives the chat. Both are
read-only: the report is built from the journal, so pressing it cannot query the
cluster. A repeat "подробнее" within 30 seconds is refused instead of being paid
for twice.

`ignore` snoozes the fingerprint for **4 hours** (`SNOOZE_SECONDS`), and the next
firing of that fingerprint is skipped as `snoozed`; every press is written to the
journal and counted in `aiops_decisions_recorded{decision=...}`. In M1 none of
them executes anything, and the chat answers with exactly what was written down.

Chat commands: `/status` (configuration and limits), `/last` (the last incident),
`/help`; anything else is ignored on purpose.

## Metrics

The `/metrics` endpoint is a 60-line text-format registry, not a dependency, and
the Service carries `prometheus.io/scrape: "true"` so the existing
`kubernetes-service-endpoints` job picks it up. That is what makes an alert on the
*bot* possible - a triage bot that silently stops is worse than none:

| Series | Meaning |
| ------ | ------- |
| `aiops_alerts_received{status}` | what Alertmanager delivered |
| `aiops_alerts_skipped{reason}` | `resolved`, `not_labelled`, `snoozed` |
| `aiops_alerts_deduped` | dropped inside the 30-minute window |
| `aiops_incidents{action_id}` / `aiops_escalations{reason}` | what was decided |
| `aiops_incidents_tier{tier}` | `rules` or `model` - the cascade, measured |
| `aiops_model_calls{adapter}` / `aiops_model_errors{adapter}` | cost and failure, per adapter |
| `aiops_schema_violations{kind}` | the model answered, but not per contract |
| `aiops_context_failures{source}` | Prometheus or Loki went quiet |
| `aiops_telegram_sent` / `aiops_telegram_suppressed` | delivered, or rate-limited |
| `aiops_telegram_documents` | reports delivered as files |
| `aiops_decisions_recorded{decision}` | human button presses |
| `aiops_build_info{version,tier1}` | always `1` - the sample that proves an idle `/metrics` is alive |
| `aiops_last_incident_timestamp_seconds` | freshness - the gauge a "silent bot" alert reads |

## Tests and CI

```powershell
cd aiops\triage-bot
python -m unittest discover -s tests -t . -v      # 136 tests, seconds, no network
```

| Test file | What it pins |
| --------- | ------------ |
| `test_model.py` | the decision schema: unknown action, missing key, `replicas: 0`, prose instead of JSON, `proposal` without `escalate_human` |
| `test_adapters.py` | the ruleset, `PREMISES` (a contradiction escalates, a missing metric does not), the provider table, the prose `answer()` path |
| `test_context.py` | the PromQL queries, the **Loki label names** (the bug), `missing` on every failure, the `desired=0` wording |
| `test_pipeline.py` | the cascade order, dedupe, snooze, tier metrics, escalation reasons, the two follow-up buttons |
| `test_report.py` | the report: cause -> grounds -> step, the hidden cascade, human presses in order, tables that survive a `\|` |
| `test_telegram.py` | the card, four buttons for an action and three for an escalation, the hand-built `sendDocument` multipart |
| `test_server.py` | the five routes, `400` on a bad body, `200` on a failed triage |
| `test_coverage.py` | every shipped rule with `aiops="true"` has a ruleset answer - and vice versa |
| `test_manifests.py` | 15 checks between the YAML and the code: ConfigMap keys vs `config.py`, the image tag vs `deploy.ps1`, the guardrails, the Alertmanager webhook URL |

CI runs this suite in the `aiops-tests` job, then greps the M1 guardrails out of
the YAML in a *second, independent* way (`automountServiceAccountToken: false`, no
`serviceAccountName`, no RBAC kind anywhere, `readOnlyRootFilesystem: true`), and
finally builds the image and import-smokes it. A red job therefore always means our
code is wrong, never that a provider is down or a key ran out. The manifests are
also validated by `kubeconform -strict`.

## M1 limits (on purpose)

* **It executes nothing.** `dry_run` is `True` by construction, and `execute()` is
  a `NotImplementedError` whose docstring carries the M2 specification.
* **No Kubernetes read access either.** The bot sees Prometheus, Loki and the
  alert. Pod events, node conditions and `describe` output are not on the card -
  they are in `missing`, which is the honest version of the same thing.
* **One replica and an in-memory recent-incident cache.** The journal survives a
  pod restart (`/data` is the `triage-bot-journal` PVC), the cache does not; the
  journal is the source of truth, and it is what `/incidents` reads. It was an
  `emptyDir` once, and the cards outlived their data: buttons on yesterday's
  message answered "incident not found", `📄 отчёт` included. A rebuilt cluster
  starts from an empty volume by design; on that first start the bot confirms the
  Telegram updates it missed instead of answering them, so presses made while it
  was down do not become a burst of "incident not found".
* **No authentication on the webhook** - the NetworkPolicy is the credential, as
  in the alerting stack.
* **The ruleset knows only the alerts this stand fires.** A new alert is a new row
  plus a test, and the test fails first - which is the point of it.

M2 adds the executor (a separate component with its own ServiceAccount and one
narrow RBAC rule), approval-based action locking and a rollout check after the
action. The whitelist, the schema and the cascade stay exactly where they are.




