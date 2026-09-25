"""Fixture real da Spotify: prompt do CARD != label da OPCAO."""

from pathlib import Path

from job_agent_v2.ats import inspect_form

FIXTURES = Path(__file__).parent / "fixtures"
PROMPT = "Have you ever been previously employed by Spotify?"
KEY = "cards[92a51f92-d683-4f7b-be57-e25376f60abe][field0]"


def _fields():
    return {field.key: field for field in inspect_form((FIXTURES / "card_checkbox_spotify.html").read_text(encoding="utf-8")).fields}


def test_the_card_prompt_becomes_the_field_prompt_not_the_option():
    assert _fields()[KEY].prompt == PROMPT


def test_the_options_stay_apart_from_the_prompt():
    field = _fields()[KEY]
    assert field.options == ("No", "Yes - Intern", "Yes - Full Time Employment")
    assert field.required is True
    assert PROMPT not in field.options


def test_the_question_identity_uses_the_prompt_never_the_option():
    field = _fields()[KEY]
    assert field.identity == " ".join(PROMPT.casefold().split())
    assert field.identity != "no"


def test_a_text_field_keeps_its_own_prompt():
    assert _fields()["email"].prompt == "Email"
