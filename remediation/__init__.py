"""
Controlled remediation for Stage 4: an allowlisted action model, a deterministic
in-memory simulator (never real infrastructure), an executor gated on
ApprovalService.consume(), an independent verifier, and the rule for which
verified outcomes may become operational memory.
"""

from remediation.actions import ACTION_KINDS, InvalidAction, RemediationAction, parse_action
from remediation.executor import ExecutionResult, RemediationExecutor
from remediation.outcome import record_verified_outcome, retention_decision
from remediation.simulator import SCENARIOS, SimulatedEnvironment, SimulationError
from remediation.verifier import RemediationVerifier, VerificationResult

__all__ = ["ACTION_KINDS", "InvalidAction", "RemediationAction", "parse_action", "ExecutionResult",
           "RemediationExecutor", "record_verified_outcome", "retention_decision", "SCENARIOS",
           "SimulatedEnvironment", "SimulationError", "RemediationVerifier", "VerificationResult"]
