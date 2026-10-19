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
- Use escalate_human whenever evidence is missing, contradictory or the safe
  action is not obvious. Escalation is a correct answer, not a failure.
- evidence MUST quote facts from the supplied context (metric values, log
  lines, replica counts). Never invent a metric, a log line or a service name.
- params are required only for the actions that need them, and namespace must be
  sock-shop. Do not add any other keys.
- If none of the allowed actions fits, answer escalate_human AND put the fix you
  would suggest into "proposal" (plain text, at most 2 sentences). "proposal" is
  allowed only together with escalate_human: it is shown to a human, it is never
  executed, and a proposal attached to an action is rejected outright.
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


def escalate(reason: str, adapter: str = "", model: str = "",
             raw: dict | None = None, proposal: str = "") -> Decision:
    """The safe default: a human gets the incident, nothing else happens."""
    return Decision(
        action_id="escalate_human",
        confidence=0.0,
        reason=reason,
        evidence=[],
        params={},
        proposal=proposal,
        adapter=adapter,
        model=model,
        escalated=True,
        escalation_reason=reason,
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

    params = validate_params(spec, payload.get("params") or {}, settings)

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
        return escalate(reason, adapter=adapter, model=model, raw=payload,
                        proposal=proposal)

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
