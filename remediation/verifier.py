"""
remediation/verifier.py
Checks whether an executed remediation actually produced its postcondition,
by reading the simulated environment's state — never the executor's own report
of success. Approved does not mean executed, and executed does not mean verified.
"""

from dataclasses import asdict, dataclass, field
from typing import Optional

from remediation.actions import RESTART, ROLLBACK, SCALE, parse_action


@dataclass
class VerificationResult:
    verified: bool
    incident_id: str
    action: dict
    checks: list = field(default_factory=list)   # [{name, expected, observed, passed}]
    observed_state: Optional[dict] = None
    failure_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class RemediationVerifier:
    def __init__(self, simulator):
        self.simulator = simulator

    def verify(self, *, incident_id: str, action: dict, before: dict) -> VerificationResult:
        """before: the service snapshot taken immediately before execution (used to
        resolve 'previous' to the concrete version that was expected)."""
        parsed = parse_action(action)
        observed = self.simulator.snapshot(incident_id, parsed.service)
        checks = []

        def check(name, expected, actual):
            checks.append({"name": name, "expected": expected, "observed": actual, "passed": expected == actual})

        if parsed.kind == ROLLBACK:
            target = before.get("previous_version") if parsed.target_version == "previous" else parsed.target_version
            check("deployed_version", target, observed["current_version"])
        elif parsed.kind == SCALE:
            check("replicas", parsed.replicas, observed["replicas"])
        elif parsed.kind == RESTART:
            check("restarted", before.get("restarts", 0) + 1, observed["restarts"])
        # Every remediation must leave the service actually healthy.
        check("service_status", "healthy", observed["status"])
        check("db_connections_ok", True, observed["db_connections_ok"])
        check("pool_utilization_below_90", True, observed["pool_utilization"] < 90)

        failed = [c["name"] for c in checks if not c["passed"]]
        return VerificationResult(
            verified=not failed, incident_id=incident_id, action=parsed.to_payload(), checks=checks,
            observed_state=observed, failure_reason=f"checks failed: {', '.join(failed)}" if failed else None)
