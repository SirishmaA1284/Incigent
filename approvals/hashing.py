"""
approvals/hashing.py
Deterministic hashing of a proposed action payload.

The approval records the hash of the exact action a human saw. Execution is
allowed only for a payload with the same hash, so an approval for action A can
never authorize a different action B.
"""

import hashlib
import json

HASH_PREFIX = "sha256:"


def _check(value, path="payload"):
    """Allow only JSON-native types with one unambiguous serialization."""
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        # 0.1 vs 0.10000000000000001, NaN, -0.0: floats are rejected so the
        # same logical action can never hash two different ways.
        raise TypeError(f"{path}: floats are not allowed in action payloads; use strings or integers")
    if isinstance(value, list):
        for i, item in enumerate(value):
            _check(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path}: keys must be strings, got {type(key).__name__}")
            _check(item, f"{path}.{key}")
        return
    raise TypeError(f"{path}: unsupported type {type(value).__name__}")


def canonical_json(payload: dict) -> str:
    """Stable JSON: sorted keys, no whitespace, UTF-8 preserved."""
    if not isinstance(payload, dict):
        raise TypeError("action payload must be a JSON object")
    _check(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def action_hash(payload: dict) -> str:
    return HASH_PREFIX + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
