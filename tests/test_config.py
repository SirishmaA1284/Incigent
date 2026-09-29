"""
tests/test_config.py — Live tests (real OpenAI or Hindsight calls) must be opt-in.
"""


def test_live_markers_are_registered(request):
    markers = request.config.getini("markers")
    assert any(m.startswith("live:") for m in markers)
    assert any(m.startswith("hindsight_live:") for m in markers)


def test_live_tests_are_deselected_by_default(request):
    # pytest.ini's addopts carries the default; `pytest -m live` overrides it.
    assert request.config.getini("addopts")[-2:] == ["-m", "not live and not hindsight_live"]


def test_default_state_databases_live_in_the_git_ignored_var_dir():
    """Checkpoints and approvals hold sensitive incident state; their default
    location must be var/, which .gitignore excludes."""
    from pathlib import Path
    import api.main as api_main
    import approvals

    for default in (api_main.DEFAULT_CHECKPOINT_DB, approvals.DEFAULT_DB_PATH):
        assert Path(default).parts[0] == "var", default
    ignore_lines = (Path(__file__).parent.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "var/" in [line.strip() for line in ignore_lines]
