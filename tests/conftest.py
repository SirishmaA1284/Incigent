"""
Test-wide safety net: normal tests must never contact a real Hindsight server,
even when .env holds real HINDSIGHT_* credentials (the app loads .env on import).

Every real Hindsight call goes through hindsight_client.Hindsight. For any test
NOT marked `hindsight_live`, that class is swapped for one whose constructor
refuses to run, and any attempt fails the test at teardown. Tests that exercise
memory should use FakeMemory or a stub client instead.
"""

import pytest

try:
    import hindsight_client as _hindsight_client
except ImportError:  # client not installed: real access is impossible anyway
    _hindsight_client = None


@pytest.fixture(autouse=True)
def isolated_state_dbs(tmp_path, monkeypatch):
    """Approval and workflow-checkpoint SQLite files go to a per-test temp dir,
    never into the repository's var/ directory."""
    monkeypatch.setenv("APPROVAL_DB_PATH", str(tmp_path / "approvals.sqlite3"))
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(tmp_path / "checkpoints.sqlite3"))
    # Tests only ever inspect temporary fixture repositories they create
    # themselves; a TARGET_REPO_PATH from .env must never leak into a test.
    monkeypatch.delenv("TARGET_REPO_PATH", raising=False)
    monkeypatch.delenv("TARGET_REPO_ID", raising=False)


class RealHindsightBlocked(RuntimeError):
    """Raised when a normal (non-live) test tries to build a real Hindsight client."""


@pytest.fixture(autouse=True)
def hindsight_guard(request, monkeypatch):
    """Yields the list of blocked attempts. A test that deliberately triggers the
    guard must assert on the list and then clear() it."""
    attempts = []
    if request.node.get_closest_marker("hindsight_live") or _hindsight_client is None:
        yield attempts
        return

    real = _hindsight_client.Hindsight

    class BlockedHindsight(real):  # subclass keeps method signatures inspectable
        def __init__(self, base_url, *args, **kwargs):
            attempts.append(base_url)
            raise RealHindsightBlocked(
                f"normal tests must not contact real Hindsight ({base_url}); "
                "use FakeMemory or mark the test hindsight_live")

    monkeypatch.setattr(_hindsight_client, "Hindsight", BlockedHindsight)
    yield attempts
    if attempts:
        pytest.fail(f"test tried to contact real Hindsight {len(attempts)} time(s): {attempts}")
