"""A correcao copiada do V1, com teste proprio (sem depender do V1)."""

from job_agent_v2.ats.lever import _css_string_escape


def test_a_control_character_becomes_a_css_escape():
    escaped = _css_string_escape("countrySurvey\n\n_all-opportunity-locations")
    assert "\n" not in escaped
    assert "\\a " in escaped
    assert "countrySurvey" in escaped and "_all-opportunity-locations" in escaped


def test_quotes_and_backslashes_keep_the_classic_escape():
    assert _css_string_escape('a"b') == 'a\\"b'
    assert _css_string_escape("a\\b") == "a\\\\b"
