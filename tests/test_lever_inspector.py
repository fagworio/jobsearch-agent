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


# --- REAL-APPLY-001 (Lever demo): id de ancestral com NEWLINE -----------------
#
# O ATS real monta o id do survey com quebras de linha DENTRO do valor. O seletor
# gerado (`[id="countrySurvey\n\n_all-..."]`) era CSS invalido, o parser recusava e
# o ciclo parava em "refusing stale or invalid form": zero POST, e tambem sem
# review. Nao se normaliza o valor (colapsar espaco apontaria para outro elemento);
# o que muda e o ESCAPE.
#
# O seletor vive em `DOMFieldBinding` (contrato de browser/CSS), NAO em
# `ApplicationField` (contrato semantico do formulario). O teste respeita a
# separacao e le pelo caminho publico `bindings.for_field(...)`.

SURVEY_NAME = "surveysResponses[8dfb36ea-fd79-4bea-aa2d-734a7b290c35][responses][field0]"


def _survey_html() -> str:
    return (FIXTURES / "survey_newline_id.html").read_text(encoding="utf-8")


def _survey_inspected():
    return ATSInspector().inspect_html(
        _survey_html(), url="https://jobs.lever.co/acme/1/apply", form_id="lever-form"
    )


def _survey_binding():
    binding = _survey_inspected().bindings.for_field(SURVEY_NAME)
    assert binding is not None, f"sem binding para {SURVEY_NAME}"
    return binding


def test_the_newline_id_binding_keeps_the_value_and_escapes_the_control_character():
    locator = _survey_binding().locator

    assert "\n" not in locator, f"controle cru no seletor: {locator!r}"
    assert "\\a " in locator, f"newline tinha de virar escape CSS: {locator!r}"
    assert "countrySurvey" in locator and "_all-opportunity-locations" in locator, "valor preservado"


def test_the_newline_id_locator_is_selected_by_a_real_css_parser_and_matches_one_input():
    import pytest as _pytest

    _pytest.importorskip("playwright.sync_api", reason="validacao de seletor exige browser")
    from playwright.sync_api import sync_playwright

    locator = _survey_binding().locator
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_context().new_page()
            page.set_content(_survey_html())
            target = page.locator(locator)
            assert target.count() == 1, f"seletor casou {target.count()} elementos: {locator!r}"
            assert target.get_attribute("name") == SURVEY_NAME
        finally:
            browser.close()
