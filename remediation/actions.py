"""
remediation/actions.py
The allowlisted remediation actions and their strict validation.

An action is plain data. It names one of three simulated operations and exactly
the fields that operation needs; anything else is rejected. There is no generic
"command" action and no field is ever interpreted as a command.

The action dict is embedded, unchanged, in the Stage 3 approval payload under
"remediation", so the approval's action hash covers it exactly.
"""

import re
from dataclasses import dataclass
from typing import Optional

RESTART = "restart_service"
ROLLBACK = "rollback_deployment"
SCALE = "scale_service"

# kind -> exact set of allowed keys (no more, no fewer)
ACTION_FIELDS = {
    RESTART: frozenset({"kind", "service"}),
    ROLLBACK: frozenset({"kind", "service", "target_version"}),
    SCALE: frozenset({"kind", "service", "replicas"}),
}
ACTION_KINDS = tuple(ACTION_FIELDS)

MIN_REPLICAS, MAX_REPLICAS = 1, 20
_SERVICE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_VERSION = re.compile(r"^v?\d{1,4}(\.\d{1,4}){0,3}$")


class InvalidAction(ValueError):
    """The payload is not an allowlisted, well-formed remediation action."""


@dataclass(frozen=True)
class RemediationAction:
    kind: str
    service: str
    target_version: Optional[str] = None
    replicas: Optional[int] = None

    def to_payload(self) -> dict:
        """Canonical dict form (what the approval hash covers)."""
        payload = {"kind": self.kind, "service": self.service}
        if self.kind == ROLLBACK:
            payload["target_version"] = self.target_version
        if self.kind == SCALE:
            payload["replicas"] = self.replicas
        return payload


def parse_action(payload) -> RemediationAction:
    """Validate a remediation payload against the allowlist. Raises InvalidAction."""
    if not isinstance(payload, dict):
        raise InvalidAction("remediation must be an object")
    kind = payload.get("kind")
    if kind not in ACTION_FIELDS:
        raise InvalidAction(f"unsupported action kind {kind!r}; allowed: {ACTION_KINDS}")
    keys = set(payload)
    if keys != ACTION_FIELDS[kind]:
        missing, extra = ACTION_FIELDS[kind] - keys, keys - ACTION_FIELDS[kind]
        raise InvalidAction(f"{kind}: missing {sorted(missing)} / unexpected {sorted(extra)}")

    service = payload["service"]
    if not isinstance(service, str) or not _SERVICE.match(service):
        raise InvalidAction("service must be a lowercase service name like 'order-service'")

    if kind == ROLLBACK:
        target = payload["target_version"]
        if not isinstance(target, str) or not (target == "previous" or _VERSION.match(target)):
            raise InvalidAction("target_version must be 'previous' or a version like 'v2.4.0'")
        return RemediationAction(kind, service, target_version=target)

    if kind == SCALE:
        replicas = payload["replicas"]
        if isinstance(replicas, bool) or not isinstance(replicas, int) or not MIN_REPLICAS <= replicas <= MAX_REPLICAS:
            raise InvalidAction(f"replicas must be an integer from {MIN_REPLICAS} to {MAX_REPLICAS}")
        return RemediationAction(kind, service, replicas=replicas)

    return RemediationAction(kind, service)
