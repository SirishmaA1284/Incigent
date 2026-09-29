"""
tests/test_hindsight_live.py — Round trip against a REAL Hindsight server.

Deselected by default. Run with:
    HINDSIGHT_API_URL=... HINDSIGHT_TEST_BANK_ID=... [HINDSIGHT_API_KEY=...] \
        python -m pytest -m hindsight_live -v

Uses HINDSIGHT_TEST_BANK_ID (never HINDSIGHT_BANK_ID) so it cannot write test
data into the application's real memory bank.
"""

import os
import re
import time
import uuid

import pytest

from memory import MemoryRecorder
from memory.hindsight_memory import HindsightMemory


def _normalize(text: str) -> str:
    """Lowercase, treat - and _ as spaces, collapse whitespace: Hindsight may paraphrase."""
    return " ".join(re.sub(r"[-_]", " ", text.lower()).split())


# Concepts the recalled facts must preserve, tolerant of paraphrase.
REQUIRED_CONCEPTS = {
    "restart": r"\brestart",
    "rejection": r"\breject",
    "safety reason": r"\bunsafe|\bnot safe|\brisk",
    "peak traffic": r"\b(peak|high|heavy|busy)\b.{0,20}\btraffic",
    "rollback alternative": r"\broll(ed|ing)? ?back|\brevert",
    "service": r"\border service\b",
}


@pytest.mark.hindsight_live
def test_retain_then_recall_round_trip():
    pytest.importorskip("hindsight_client")
    url, bank = os.getenv("HINDSIGHT_API_URL"), os.getenv("HINDSIGHT_TEST_BANK_ID")
    if not url or not bank:
        pytest.skip("HINDSIGHT_API_URL and HINDSIGHT_TEST_BANK_ID must be set")
    if bank == os.getenv("HINDSIGHT_BANK_ID"):
        pytest.fail("HINDSIGHT_TEST_BANK_ID must differ from HINDSIGHT_BANK_ID; refusing to write test data")

    recorder = MemoryRecorder(HindsightMemory(url, bank, api_key=os.getenv("HINDSIGHT_API_KEY") or None))
    past = f"LIVE-TEST-{uuid.uuid4().hex[:8]}"
    outcome = recorder.record_human_decision(
        past, recommendation_id="rec-1", recommendation="Restart order-service", decision="rejected",
        decided_by="live test", reason="Restart was unsafe during peak traffic",
        alternative="Rollback deployment", services=["order-service"], pattern_tags=["DB_POOL_EXHAUSTED"])
    assert outcome.status == "retained", outcome.error

    query = "order-service DB_POOL_EXHAUSTED restart rejected unsafe peak traffic rollback"
    deadline = time.time() + 30
    while True:
        recalled = recorder.recall_for_incident("LIVE-TEST-current", query, limit=20)
        assert recalled.status == "available", recalled.error
        mine = [m for m in recalled.memories if m.incident_id == past]
        text = _normalize(" ".join(m.text for m in mine))
        missing = [name for name, pattern in REQUIRED_CONCEPTS.items() if not re.search(pattern, text)]
        if (mine and not missing) or time.time() > deadline:
            break
        time.sleep(2)

    # This run's document came back (unique ID via returned metadata).
    assert mine, f"no recalled memory for {past}; got incident_ids {[m.incident_id for m in recalled.memories]}"
    # The recalled facts preserve the decision, its reason and the alternative.
    assert not missing, f"recalled facts lost {missing}: {[m.text for m in mine]}"
    # Our metadata travels with every extracted fact of the document.
    assert {m.event_type for m in mine} == {"human_decision"}, [m.event_type for m in mine]
    assert all(m.source == "hindsight" for m in mine)
