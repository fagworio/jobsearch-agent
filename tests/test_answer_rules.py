"""Mechanics of the reusable answer rules.

The rules themselves are candidate data (``profile/answers.local.yaml``, which is
gitignored), so these tests exercise the *mechanism* with a temporary rules file:
matching by meaning, exact-option agreement, rule ordering, and the profile-backed
``current_company`` source.
"""

from pathlib import Path

import yaml

from jobsearch_agent.models import ApplicationField
from jobsearch_agent.profile import load_profile
from jobsearch_agent.qa import AnswerKnowledgeBase, load_rules


ROOT = Path(__file__).parents[1]


def _knowledge(tmp_path: Path, rules: list[dict]) -> AnswerKnowledgeBase:
    path = tmp_path / "answers.yaml"
    path.write_text(yaml.safe_dump({"rules": rules}, allow_unicode=True), encoding="utf-8")
    return AnswerKnowledgeBase([], load_rules(path))


def _select(label: str, options: list[str], required: bool = True) -> ApplicationField:
    return ApplicationField(
        key=f"cards[{abs(hash(label))}][field0]",
        label=label,
        field_type="select",
        required=required,
        options=options,
        confidence=0.0,
    )


def test_rule_answer_must_match_an_option_of_the_same_field(tmp_path):
    kb = _knowledge(
        tmp_path,
        [{"name": "family_at_company", "match": ["algum familiar"], "answer": "Não"}],
    )
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    field = _select(
        "Você possui algum familiar que atualmente trabalha na CI&T?",
        ["Select...", "Sim", "Não"],
    )
    answer = kb.resolve_field(field, profile)
    assert answer is not None
    assert answer.answer == "Não"


def test_rule_is_dropped_when_its_answer_is_not_an_option(tmp_path):
    """A 'Sim' answer must never be typed into a field whose options lack it."""
    kb = _knowledge(tmp_path, [{"name": "family_at_company", "match": ["algum familiar"], "answer": "Sim"}])
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    field = _select("Você possui algum familiar na CI&T?", ["Select...", "Não"])
    assert kb.resolve_field(field, profile) is None


def test_specific_language_rule_wins_over_generic_one(tmp_path):
    """Rule order decides: the level question and the comfort question share wording."""
    level = "Avançado. Exemplo: Consigo participar ativamente de conversas sobre temas familiares."
    kb = _knowledge(
        tmp_path,
        [
            {
                "name": "english_level",
                "match": ["onde se fala ingles"],
                "answer": level,
            },
            {"name": "english_comfort", "match": ["se sente confortavel"], "answer": "Sim"},
        ],
    )
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    field = _select(
        "[EN] Qual é o seu nível de confiança ao se comunicar e colaborar em um ambiente de trabalho onde se fala inglês?",
        ["Select...", "Básico. Exemplo: Entendo frases básicas e consigo me apresentar.", level],
    )
    answer = kb.resolve_field(field, profile)
    assert answer is not None
    assert answer.answer == level


def test_current_company_source_reads_the_profile_experience(tmp_path):
    kb = _knowledge(
        tmp_path,
        [
            {
                "name": "current_company_name",
                "match": ["empresa em que voce trabalha"],
                "from": "current_company",
            }
        ],
    )
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    field = ApplicationField(
        key="cards[x][field0]",
        label="Confirme o nome da empresa em que você trabalha atualmente",
        field_type="text",
        required=True,
        confidence=0.0,
    )
    answer = kb.resolve_field(field, profile)
    assert answer is not None
    open_roles = [item for item in profile.experiences if not item.end_date]
    expected = max(open_roles, key=lambda item: (item.start_date or "", item.end_date or "")).company
    assert answer.answer == expected
    assert answer.supported_by == ["answer_policy:current_company_name"]


def test_consent_checkbox_accepts_a_boolean_rule_answer(tmp_path):
    kb = _knowledge(
        tmp_path,
        [{"name": "data_storage_consent", "match": ["dados pessoais sejam armazenados"], "answer": "true"}],
    )
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    field = ApplicationField(
        key="consent[store]",
        label="Concordo que meus dados pessoais sejam armazenados e processados pela CI&T.",
        field_type="checkbox",
        semantic_type="checkbox_boolean",
        required=True,
        confidence=0.0,
    )
    answer = kb.resolve_field(field, profile)
    assert answer is not None
    assert answer.answer == "true"


def test_text_rule_never_lands_in_a_boolean_checkbox(tmp_path):
    """A consent sentence that merely mentions 'email' must not receive the address."""
    kb = _knowledge(tmp_path, [{"name": "email", "match": ["email"], "answer": "me@example.com"}])
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    field = ApplicationField(
        key="consent[marketing]",
        label="Autorizo o envio de informacoes por midias sociais, telefone e email sobre novas oportunidades",
        field_type="checkbox",
        semantic_type="checkbox_boolean",
        required=False,
        confidence=0.0,
    )
    assert kb.resolve_field(field, profile) is None


def test_boolean_answer_still_reaches_a_boolean_checkbox(tmp_path):
    kb = _knowledge(tmp_path, [{"name": "opt_in", "match": ["concordo"], "answer": "true"}])
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    field = ApplicationField(
        key="consent[store]",
        label="Concordo que meus dados sejam armazenados",
        field_type="checkbox",
        semantic_type="checkbox_boolean",
        required=True,
        confidence=0.0,
    )
    answer = kb.resolve_field(field, profile)
    assert answer is not None and answer.answer == "true"


def test_blank_optional_checkbox_is_valid_not_invalid():
    """An untouched optional opt-in must not block the whole application."""
    from jobsearch_agent.forms import validate_application_field

    optional = ApplicationField(
        key="consent[marketing]",
        label="Autorizo o envio de novidades",
        field_type="checkbox",
        semantic_type="checkbox_boolean",
        required=False,
    )
    assert validate_application_field(optional).valid is True

    required = ApplicationField(
        key="consent[store]",
        label="Concordo com o armazenamento",
        field_type="checkbox",
        semantic_type="checkbox_boolean",
        required=True,
    )
    result = validate_application_field(required)
    assert result.valid is False
    assert result.code == "MISSING_VALUE"
