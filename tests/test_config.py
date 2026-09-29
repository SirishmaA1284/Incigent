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
