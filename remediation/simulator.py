"""
remediation/simulator.py
A deterministic, in-memory simulation of the services involved in an incident.

It is ordinary Python state. It never touches real infrastructure: no shell,
subprocess, network, containers, cluster clients or files. Each incident has
its own isolated environment built from a named scenario.

Fault models (what makes a service unhealthy, and what can fix it):
  bad_version  - the current deployment leaks connections; only moving off
                 `bad_version` heals it. A restart briefly frees the pool but
                 the leak returns, so the service stays unhealthy.
  transient    - pool exhaustion that a restart clears.
  capacity     - too few replicas; healthy once replicas >= required_replicas.
"""

import copy
from dataclasses import asdict, dataclass, field
from typing import Optional

from remediation.actions import RESTART, ROLLBACK, SCALE, RemediationAction


class SimulationError(Exception):
    """A simulated operation was refused (unknown incident/service, reused approval)."""


@dataclass
class ServiceState:
    name: str
    status: str                       # "healthy" | "unhealthy"
    current_version: str
    previous_version: Optional[str]   # None: no rollback target recorded
    available_versions: list          # versions the deploy system can still deploy
    replicas: int
    max_replicas: int                 # quota; scaling above it is capped
    traffic: str                      # "peak" | "normal"
    pool_utilization: int             # percent
    db_connections_ok: bool
    condition: str                    # human-readable incident condition
    fault: str                        # "bad_version" | "transient" | "capacity" | "none"
    bad_version: Optional[str] = None
    required_replicas: int = 1
    restarts: int = 0


def _recompute(svc: ServiceState) -> None:
    if svc.fault == "bad_version":
        healthy = svc.current_version != svc.bad_version
    elif svc.fault == "capacity":
        healthy = svc.replicas >= svc.required_replicas
    elif svc.fault == "transient":
        healthy = False
    else:
        healthy = True
    svc.status = "healthy" if healthy else "unhealthy"
    svc.pool_utilization = 35 if healthy else 100
    svc.db_connections_ok = healthy
    svc.condition = "normal operation" if healthy else svc.condition


def _order_service(**overrides) -> ServiceState:
    base = dict(
        name="order-service", status="unhealthy", current_version="v2.4.1", previous_version="v2.4.0",
        available_versions=["v2.3.9", "v2.4.0", "v2.4.1"], replicas=2, max_replicas=6, traffic="peak",
        pool_utilization=100, db_connections_ok=False,
        condition="database connection pool exhausted", fault="bad_version", bad_version="v2.4.1",
    )
    base.update(overrides)
    return ServiceState(**base)


# Deterministic scenarios. Each builds fresh state every time it is loaded.
SCENARIOS = {
    # Connection leak shipped in v2.4.1: rollback heals; restart and scale do not.
    "INC-SIM-001": lambda: [_order_service()],
    # Transient pool exhaustion: a restart heals.
    "INC-SIM-002": lambda: [_order_service(fault="transient", bad_version=None, traffic="normal")],
    # Leak in v2.4.1 but no rollback target is available.
    "INC-SIM-003": lambda: [_order_service(previous_version=None, available_versions=["v2.4.1"])],
    # Under-provisioned: needs 4 replicas, quota allows 6.
    "INC-SIM-004": lambda: [_order_service(fault="capacity", bad_version=None, required_replicas=4,
                                           condition="request queue saturated; too few replicas")],
}


@dataclass
class IncidentEnvironment:
    incident_id: str
    services: dict = field(default_factory=dict)
    applied: list = field(default_factory=list)   # [(approval_id, action payload)], append-only


class SimulatedEnvironment:
    def __init__(self, scenarios=None):
        self._scenarios = dict(SCENARIOS if scenarios is None else scenarios)
        self._envs = {}

    def load(self, incident_id: str) -> IncidentEnvironment:
        """Return the incident's environment, building it from its scenario once."""
        if incident_id not in self._envs:
            if incident_id not in self._scenarios:
                raise SimulationError(f"no simulated environment for incident {incident_id!r}")
            services = {s.name: s for s in self._scenarios[incident_id]()}
            self._envs[incident_id] = IncidentEnvironment(incident_id, services)
        return self._envs[incident_id]

    def reset(self, incident_id: str) -> None:
        self._envs.pop(incident_id, None)

    def has_service(self, incident_id: str, service: str) -> bool:
        try:
            return service in self.load(incident_id).services
        except SimulationError:
            return False

    def snapshot(self, incident_id: str, service: Optional[str] = None) -> dict:
        """A deep copy of observable state; callers can never mutate the simulator through it."""
        env = self.load(incident_id)
        services = {n: asdict(s) for n, s in env.services.items()}
        if service is not None:
            return copy.deepcopy(services[service])
        return {"incident_id": incident_id, "services": services,
                "applied": [{"approval_id": a, "action": dict(p)} for a, p in env.applied]}

    def apply(self, action: RemediationAction, *, incident_id: str, approval_id: str) -> dict:
        """Apply one approved action. Refuses a second application for the same approval."""
        env = self.load(incident_id)
        if any(a == approval_id for a, _ in env.applied):
            raise SimulationError(f"approval {approval_id} was already applied")
        svc = env.services.get(action.service)
        if svc is None:
            raise SimulationError(f"service {action.service!r} is not part of incident {incident_id}")

        effect = {"applied": True}
        if action.kind == RESTART:
            svc.restarts += 1
            if svc.fault == "transient":
                svc.fault = "none"
        elif action.kind == ROLLBACK:
            target = svc.previous_version if action.target_version == "previous" else action.target_version
            if target is None or target not in svc.available_versions:
                effect = {"applied": False, "reason": "target_version_unavailable", "target": target}
            else:
                effect["from_version"], effect["to_version"] = svc.current_version, target
                svc.previous_version, svc.current_version = svc.current_version, target
        elif action.kind == SCALE:
            svc.replicas = min(action.replicas, svc.max_replicas)
            if svc.replicas < action.replicas:
                effect["capped_at"] = svc.max_replicas
        else:  # unreachable: actions are validated against the allowlist
            raise SimulationError(f"unsupported action {action.kind!r}")
        _recompute(svc)
        env.applied.append((approval_id, action.to_payload()))
        return effect
