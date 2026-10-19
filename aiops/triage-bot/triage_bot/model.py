"""The decision contract: schema, action whitelist and prompt.

The model never executes anything, and neither does its free text. It returns a
JSON object carrying an `action_id`; that id is looked up in the table below and
the command M2 *would* run is rendered **by this code** from validated
parameters. A model that invents an action, omits a field, answers in prose or
simply is not sure produces an escalation to a human - not a retry loop, and
definitely not an action.

Why the whitelist lives in code and not in a ConfigMap: it is the security
boundary of the whole component. A file that anyone with ConfigMap access can
edit would turn "the model cannot do anything unapproved" into "the model cannot
do anything outside the list somebody else chose last week".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Kubernetes object names: DNS-1123 labels. Validated on purpose - these values
# are interpolated into a shell command in M2, so a stray `;` must be impossible
# long before that command exists.
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

#: Keys a decision may contain. Anything else is rejected (strict, no extras):
#: a model that returns "extra" fields is a model that is inventing structure.
REQUIRED_KEYS = ("action_id", "confidence", "reason", "evidence")
#: `proposal` is the one place where free text is allowed - and only together
#: with `escalate_human`. It is displayed to a human and never executed: there is
#: no code path anywhere that turns it into a command.
OPTIONAL_KEYS = ("params", "proposal")
ALLOWED_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS
#: Anything longer than this is truncated: the card is a notification, not a
#: document, and an unbounded field is a cheap way to blow up a Telegram message.
MAX_PROPOSAL_CHARS = 500


@dataclass(frozen=True)
class ActionSpec:
    id: str
    description: str
    risk: str
    requires_approval: bool
    command: str
    params: tuple = ()

    @property
    def is_escalation(self) -> bool:
        return self.id == "escalate_human"


ACTIONS: dict[str, ActionSpec] = {
    spec.id: spec
    for spec in (
        ActionSpec(
            id="restart_deployment",
            description="Rolling restart of one Deployment (idempotent, no data loss).",
            risk="low",
            requires_approval=True,
            command="kubectl -n {namespace} rollout restart deployment/{deployment}",
            params=("namespace", "deployment"),
        ),
        ActionSpec(
            id="rollback_deployment",
            description="Roll back one Deployment to its previous revision.",
            risk="medium",
            requires_approval=True,
            command="kubectl -n {namespace} rollout undo deployment/{deployment}",
            params=("namespace", "deployment"),
        ),
        ActionSpec(
            id="scale_deployment",
            description="Set an explicit replica count (1..10) on one Deployment.",
            risk="medium",
            requires_approval=True,
            command=("kubectl -n {namespace} scale deployment/{deployment} "
                     "--replicas={replicas}"),
            params=("namespace", "deployment", "replicas"),
        ),
        ActionSpec(
            id="escalate_human",
            description="No safe automated step: hand the incident to a human.",
            risk="none",
            requires_approval=False,
            command="",
            params=(),
        ),
    )
}

SYSTEM_PROMPT = """You are an SRE assistant for a Kubernetes demo stand (namespace
sock-shop). You receive one firing alert and read-only evidence collected from
Prometheus and Loki.

Answer with ONE JSON object and nothing else - no prose, no markdown fences:
{"action_id": "<one id from the allowed list>", "confidence": <0.0-1.0>,
 "reason": "<1-3 sentences, in Russian>", "evidence": ["<fact>", ...],
 "params": {"namespace": "...", "deployment": "...", "replicas": <int>}}

Rules:
- action_id MUST be one of: {action_ids}.
- reason MUST name the likely cause and the chain that leads to it: what the
  alert says -> what the evidence shows -> which object is broken and why the
  service fails. "Something is wrong, an engineer should look" is not a reason;
  write it for a person who has 30 seconds and cannot see this prompt.
- evidence MUST quote facts from the supplied context (metric values, log
  lines, replica counts). Never invent a metric, a log line or a service name.
  If the context names no object, say so instead of guessing a name.
- Use escalate_human whenever evidence is missing, contradictory or the safe
  action is not obvious. Escalation is a correct answer, not a failure: its
  reason still names the cause, and its "proposal" carries the concrete next
  step - including the exact kubectl command when you can tell which object and
  which fix are meant. A human reads the proposal; nothing executes it.
- params belong only to the actions that take them: never send params together
  with escalate_human. They are dropped, and the note makes the report worse.
- namespace must be sock-shop. Do not add any other keys.
- "proposal" is allowed only together with escalate_human: it is shown to a
  human, it is never executed, and a proposal attached to an action is rejected.
- You cannot run anything. A human approves every action."""


def system_prompt() -> str:
    # `replace`, not `format`: the prompt contains literal JSON braces, and
    # str.format would treat them as fields.
    return SYSTEM_PROMPT.replace("{action_ids}", ", ".join(sorted(ACTIONS)))


@dataclass
class Decision:
    action_id: str
    confidence: float
    reason: str
    evidence: list = field(default_factory=list)
    params: dict = field(default_factory=dict)
    #: Free-text suggestion shown to a human when nothing in the whitelist fits.
    proposal: str = ""
    adapter: str = ""
    model: str = ""
    escalated: bool = False
    escalation_reason: str = ""
    #: What the validator changed or tolerated, shown on the card. A note is not
    #: an error: it records e.g. params dropped next to an escalation, so the
    #: bot never quietly repairs a model's answer.
    validation_note: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def spec(self) -> ActionSpec:
        return ACTIONS[self.action_id]

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "confidence": round(float(self.confidence), 2),
            "reason": self.reason,
            "evidence": list(self.evidence),
            "params": dict(self.params),
            "proposal": self.proposal,
            "command": render_command(self),
            "adapter": self.adapter,
            "model": self.model,
            "escalated": self.escalated,
            "escalation_reason": self.escalation_reason,
            "validation_note": self.validation_note,
        }


class DecisionError(Exception):
    """A decision that violates the contract. The pipeline escalates on this."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def render_command(decision: Decision) -> str:
    """The exact command M2 would run - rendered here, from validated params."""
    spec = ACTIONS[decision.action_id]
    if not spec.command:
        return ""
    return spec.command.format(**decision.params)


def build_user_prompt(alert: dict, context: dict) -> str:
    """Render the alert plus the collected evidence as one compact text block."""
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    lines = ["ALERT", json.dumps(
        {
            "name": labels.get("alertname", ""),
            "status": alert.get("status", ""),
            "severity": labels.get("severity", ""),
            "labels": labels,
            "summary": annotations.get("summary", ""),
            "description": annotations.get("description", ""),
            "runbook": annotations.get("runbook", ""),
            "startsAt": alert.get("startsAt", ""),
        }, ensure_ascii=False, indent=2)]

    lines.append("CONTEXT")
    for source in ("prometheus", "loki"):
        block = context.get(source)
        if not block:
            continue
        lines.append(f"[{source}]")
        if isinstance(block, dict):
            for key, value in block.items():
                lines.append(
                    f"- {key}: {json.dumps(value, ensure_ascii=False)}"[:600])
        else:
            for item in list(block)[:60]:
                lines.append(f"- {item}")
    missing = context.get("missing") or []
    if missing:
        lines.append("[missing] " + "; ".join(str(m) for m in missing))
    return "\n".join(lines)


ADVICE_PROMPT = """You are the same SRE assistant, now answering a follow-up about one
incident you have already triaged. The engineer has the card; you get the same
facts and nothing else.

Write plain text in Russian, at most 12 short lines. No JSON, no markdown:
- 2-4 hypotheses, most likely first, one line each: what it is and which fact
  from the incident supports it;
- then a line "Проверить:" and concrete commands (kubectl, PromQL, LogQL) that
  would confirm or reject them, one per line;
- then a line "Отбросить, если:" and one line per hypothesis saying what would
  make it wrong.

Rules:
- Use only the facts in the incident. If something is unknown, name what is
  unknown instead of inventing a service, a metric or a log line.
- You still cannot run anything: every command is for a human to run.
- No preamble, no restating the card, no apology."""


def advice_prompt() -> str:
    return ADVICE_PROMPT


def build_advice_prompt(incident: dict) -> str:
    """The recorded incident, compact: a follow-up must not need the cluster.

    Everything here was collected before the human pressed the button, so this
    call cannot widen what the bot saw - it only re-reads its own journal entry.
    """
    labels = incident.get("labels") or {}
    annotations = incident.get("annotations") or {}
    final = incident.get("final") or {}
    lines = ["INCIDENT", json.dumps({
        "id": incident.get("id", ""),
        "alertname": incident.get("alertname", ""),
        "labels": labels,
        "summary": annotations.get("summary", ""),
        "description": annotations.get("description", ""),
        "runbook": annotations.get("runbook", ""),
        "reason": final.get("reason", ""),
        "command": final.get("command", ""),
        "proposal": final.get("proposal", ""),
        "executable": bool(incident.get("executable")),
    }, ensure_ascii=False, indent=2)]

    evidence = final.get("evidence") or []
    if evidence:
        lines.append("EVIDENCE")
        lines.extend(f"- {item}" for item in evidence[:8])
    missing = incident.get("missing") or []
    if missing:
        lines.append("NOT AVAILABLE: " + "; ".join(str(item) for item in missing))
    return "\n".join(lines)


def escalate(reason: str, adapter: str = "", model: str = "",
             raw: dict | None = None, proposal: str = "",
             evidence: list | None = None, confidence: float = 0.0,
             note: str = "") -> Decision:
    """The safe default: a human gets the incident, nothing else happens.

    `evidence` and `confidence` are carried through on purpose. The live stand
    once sent a card that said "нужен инженер" and nothing else: the decision was
    right, but the facts it rested on had already been collected and were thrown
    away right here. An escalation is a report, not an apology.
    """
    return Decision(
        action_id="escalate_human",
        confidence=confidence,
        reason=reason,
        evidence=list(evidence or []),
        params={},
        proposal=proposal,
        adapter=adapter,
        model=model,
        escalated=True,
        escalation_reason=reason,
        validation_note=note,
        raw=raw or {},
    )


def parse_decision_json(text: str) -> dict:
    """Parse the model output into a JSON object, or raise DecisionError."""
    if not isinstance(text, str) or not text.strip():
        raise DecisionError("not_json", "the model returned nothing")
    candidate = text.strip()
    # Models like to wrap JSON in markdown fences; stripping them is tolerance,
    # not forgiveness - everything after this point is validated strictly.
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise DecisionError("not_json", f"the model output is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DecisionError("schema", "the model returned JSON that is not an object")
    return payload


def validate_params(spec: ActionSpec, raw_params: dict, settings) -> dict:
    """Validate action parameters against the whitelist entry and the policy."""
    if not isinstance(raw_params, dict):
        raise DecisionError("bad_params", "params must be an object")
    expected = set(spec.params)
    given = set(raw_params)
    if given != expected:
        raise DecisionError(
            "bad_params",
            f"{spec.id} needs exactly {sorted(expected) or 'no params'}, "
            f"got {sorted(given) or 'none'}")

    params: dict = {}
    for name in ("namespace", "deployment"):
        if name in raw_params:
            value = raw_params[name]
            if not isinstance(value, str) or not _NAME_RE.match(value or ""):
                raise DecisionError(
                    "bad_params", f"{name}={value!r} is not a valid Kubernetes name")
            params[name] = value
    if "replicas" in raw_params:
        value = raw_params["replicas"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise DecisionError("bad_params", f"replicas={value!r} is not an integer")
        if value < 1 or value > 10:
            # Scaling to 0 is a deliberate outage; the bot does not do that.
            raise DecisionError("bad_params", f"replicas={value} is outside 1..10")

    namespace = params.get("namespace")
    if namespace is not None and settings.allowed_namespaces:
        if namespace not in settings.allowed_namespaces:
            raise DecisionError(
                "bad_params",
                f"namespace {namespace!r} is outside the allowed namespaces "
                f"{list(settings.allowed_namespaces)}")
    return params


def validate_decision(payload: dict, settings, adapter: str = "",
                      model: str = "") -> Decision:
    """Turn a raw model answer into a Decision, or raise DecisionError.

    Every rejection reason is a *policy* answer: the pipeline converts it into an
    escalation, so a model that misbehaves can never produce an action.
    """
    extra = sorted(set(payload) - set(ALLOWED_KEYS))
    if extra:
        raise DecisionError("schema", f"unexpected keys in the answer: {extra}")
    missing = [key for key in REQUIRED_KEYS if key not in payload]
    if missing:
        raise DecisionError("schema", f"missing required keys: {missing}")

    action_id = payload["action_id"]
    if not isinstance(action_id, str) or action_id not in ACTIONS:
        raise DecisionError(
            "unknown_action",
            f"action_id {action_id!r} is not in the whitelist {sorted(ACTIONS)}")
    spec = ACTIONS[action_id]

    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise DecisionError("schema", f"confidence={confidence!r} is not a number")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise DecisionError("schema", f"confidence={confidence} is outside 0..1")

    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise DecisionError("schema", "reason must be a non-empty string")
    reason = reason.strip()
    if len(reason) > 600:
        reason = reason[:597] + "..."

    evidence = payload["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise DecisionError("schema", "evidence must be a non-empty list")
    clean_evidence = [str(item).strip()[:300] for item in evidence if str(item).strip()]
    if not clean_evidence:
        raise DecisionError("schema", "evidence entries must not be empty")
    clean_evidence = clean_evidence[:settings.max_evidence_items]

    # `escalate_human` executes nothing, so params next to it cannot cause harm -
    # but rejecting the whole answer (M1 did, and a live DeepSeek answer hit it)
    # replaced the model's cause and proposal with a validator error on the card.
    # The params are dropped, and the fact is stated in `validation_note`.
    note = ""
    raw_params = payload.get("params") or {}
    if spec.is_escalation and raw_params:
        note = "params ignored: escalate_human takes none"
        raw_params = {}
    params = validate_params(spec, raw_params, settings)

    proposal = payload.get("proposal") or ""
    if proposal:
        if not isinstance(proposal, str):
            raise DecisionError("schema", "proposal must be a string")
        proposal = proposal.strip()[:MAX_PROPOSAL_CHARS]
        if not spec.is_escalation:
            # A proposal next to an action means the model is unsure what it is
            # doing, and an out-of-whitelist fix must never travel together with
            # an executable action. Reject the whole answer.
            raise DecisionError(
                "schema",
                f"proposal is only allowed with escalate_human, not with {action_id}")

    if confidence < settings.confidence_threshold and not spec.is_escalation:
        raise DecisionError(
            "low_confidence",
            f"confidence {confidence:.2f} is below the threshold "
            f"{settings.confidence_threshold:.2f} - a human decides")

    if spec.is_escalation:
        # Keep the facts and the confidence: the escalation is the report, and a
        # report without evidence is what this card used to be.
        return escalate(reason, adapter=adapter, model=model, raw=payload,
                        proposal=proposal, evidence=clean_evidence,
                        confidence=confidence, note=note)

    return Decision(
        action_id=action_id,
        confidence=confidence,
        reason=reason,
        evidence=clean_evidence,
        params=params,
        adapter=adapter,
        model=model,
        escalated=False,
        raw=payload,
    )
