"""
tests/test_config.py — Live tests (real OpenAI calls) must be opt-in.
"""


def test_live_marker_is_registered(request):
    markers = request.config.getini("markers")
    assert any(m.startswith("live:") for m in markers)


def test_live_tests_are_deselected_by_default(request):
    # pytest.ini's addopts carries the default; `pytest -m live` overrides it.
    assert request.config.getini("addopts")[-2:] == ["-m", "not live"]
