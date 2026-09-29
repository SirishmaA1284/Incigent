"""
Long-term operational memory (advisory context only).

Import the recorder and types from here; only memory/hindsight_memory.py knows
the Hindsight client API.
"""

from memory.base import (
    APP_TAG, EVENT_TYPES, MemoryEvent, MemoryRecord, MemoryUnavailableError,
    OperationalMemory, RecalledMemory,
)
from memory.recorder import MemoryRecorder, RecallOutcome, RetainOutcome

__all__ = [
    "APP_TAG", "EVENT_TYPES", "MemoryEvent", "MemoryRecord", "MemoryUnavailableError",
    "OperationalMemory", "RecalledMemory", "MemoryRecorder", "RecallOutcome", "RetainOutcome",
    "build_memory_recorder",
]


def build_memory_recorder() -> MemoryRecorder:
    """Recorder backed by Hindsight when configured via env, otherwise disabled."""
    from memory.hindsight_memory import HindsightMemory
    memory, reason = HindsightMemory.from_env()
    return MemoryRecorder(memory, disabled_reason=reason)
