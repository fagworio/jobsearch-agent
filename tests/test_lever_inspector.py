"""Lever renders custom questions as ``<li class="application-question">`` cards.

The prompt lives in a sibling ``div.application-label .text`` rather than in a
``label[for]``, which used to degrade every custom question to its raw input
key (``cards[<uuid>][field0]``) and made the form unreadable to a reviewer.
"""

from pathlib import Path

from jobsearch_agent.inspector import ATSInspector


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures/lever"


def _inspect():
    html = (FIXTURES / "card_question.html").read_text(encoding="utf-8")
    return ATSInspector().inspect_html(
        html, url="https://jobs.lever.co/acme/1/apply", form_id="lever-form"
    )


def _by_key(inspected):
    return {field.key: field for field in inspected.form.fields}


def test_lever_card_prompt_becomes_the_field_label():
    field = _by_key(_inspect())["cards[901d99b3-8bc5-45de-838b-d6bcf0300654][field0]"]
    assert field.label == "Como ficou sabendo da nossa vaga?"


def test_lever_card_label_drops_the_required_marker():
    labels = {field.key: field.label for field in _inspect().form.fields}
    assert all("\u2733" not in label for label in labels.values())
    assert not any(label.startswith("cards[") for label in labels.values())


def test_lever_card_textarea_and_file_prompts_are_read():
    labels = {field.key: field.label for field in _inspect().form.fields}
    assert labels["cards[46937f62-1111-2222-3333-444455556666][field0]"] == (
        "Quais tecnologias voc\u00ea domina?"
    )
    assert labels["cards[377b7c9a-7777-8888-9999-000011112222][field0]"] == (
        "Anexe aqui o laudo m\u00e9dico"
    )


def test_lever_card_options_survive_the_label_change():
    field = _by_key(_inspect())["cards[901d99b3-8bc5-45de-838b-d6bcf0300654][field0]"]
    assert field.field_type == "select"
    assert list(field.options) == ["Selecione", "LinkedIn", "Indica\u00e7\u00e3o"]


def test_lever_card_required_flag_and_labeled_controls_still_work():
    fields = _by_key(_inspect())
    assert fields["cards[901d99b3-8bc5-45de-838b-d6bcf0300654][field0]"].required is True
    assert fields["cards[377b7c9a-7777-8888-9999-000011112222][field0]"].required is False
    # A real ``label[for]``/wrapping label must keep winning over the card path.
    assert fields["name"].label == "Nome completo"
    assert fields["consent[store]"].label == "Concordo que meus dados sejam armazenados"
