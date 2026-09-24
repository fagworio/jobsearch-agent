"""JSA-QA-001: o resolvedor unificado de perguntas.

Os cenarios QA-01..QA-12 do roadmap. O que se prova aqui, em uma frase: **toda
pergunta recebe uma decisao explicita, e nenhuma resposta nasce sem suporte.**
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsearch_agent.models import (
    ApplicationAnswer,
    ApplicationField,
    CandidatePreferences,
    CareerProfile,
    Experience,
    Job,
)
from jobsearch_agent.qa import AnswerKnowledgeBase, AnswerRule
from jobsearch_agent.resolver import (
    CandidateContextBuilder,
    FactValidator,
    GeneratedAnswer,
    GenerationConstraints,
    GroundedTemplateProvider,
    QuestionResolver,
    ResolutionStatus,
)


def _profile() -> CareerProfile:
    return CareerProfile(
        identity={
            "name": "QA Candidate",
            "first_name": "QA",
            "last_name": "Candidate",
            "email": "qa.candidate@example.invalid",
            "phone": "+55 11 90000-0000",
            "current_location": "São Paulo, Brazil",
            "linkedin": "https://www.linkedin.com/in/qa-candidate",
        },
        identity_fact_ids={"first_name": "fact_qa_first_name"},
        professional_summary={"en-US": "WordPress developer with WooCommerce experience."},
        experiences=[
            Experience(id="exp-qa-1", company="Example Studio", role="WordPress Developer", start_date="2019-03"),
            Experience(id="exp-qa-2", company="Older Agency", role="Junior Developer", start_date="2017-01", end_date="2019-02"),
        ],
        skills={
            "wordpress": {"years": "7", "level": "advanced", "tags": ["wordpress", "php"]},
            "woocommerce": {"years": "5", "level": "advanced", "tags": ["woocommerce"]},
        },
        languages={"portuguese": {"level": "native"}, "english": {"level": "advanced"}},
        demo=False,
    )


def _job(language: str = "en-US") -> Job:
    return Job(
        id="job-qa",
        source="greenhouse",
        external_id="qa-1",
        company="Acme",
        title="WordPress Developer",
        description="Build WordPress plugins and WooCommerce integrations.",
        language=language,
    )


def _resolver(
    *,
    answers: AnswerKnowledgeBase | None = None,
    preferences: CandidatePreferences | None = None,
    provider=None,
    language: str = "en-US",
    resume: dict | None = None,
    identity_profile: CareerProfile | None = None,
) -> QuestionResolver:
    return QuestionResolver(
        knowledge=answers or AnswerKnowledgeBase([]),
        profile=identity_profile or _profile(),
        preferences=preferences or CandidatePreferences(),
        job=_job(language),
        resume=resume,
        provider=provider,
    )


def _approved(question: str, answer: str) -> AnswerKnowledgeBase:
    return AnswerKnowledgeBase(
        [
            ApplicationAnswer(
                question_key=f"k-{question[:8]}",
                question=question,
                answer=answer,
                source="approved_answer",
                confidence=1.0,
                approved=True,
            )
        ]
    )


# --- QA-01..QA-05: deterministico ---------------------------------------------


def test_qa_01_explicit_identity_resolves_from_the_profile():
    resolution = _resolver().resolve_field(ApplicationField(key="first_name", label="First name"))
    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.answer == "QA"
    assert resolution.source == "CareerProfile.identity"
    assert resolution.supported_by == ("fact_qa_first_name",)


def test_qa_02_years_of_a_skill_resolve_from_the_profile():
    """Pergunta factual, fato presente: nao vai para humano nem para o modelo."""
    field = ApplicationField(key="q-years", label="How many years of WordPress experience do you have?")
    resolution = _resolver().resolve_field(field)

    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.answer == "7"
    assert resolution.semantic_type == "experience_years"
    assert resolution.supported_by == ("profile.skills.wordpress",)
    assert resolution.generated is False


def test_qa_03_work_authorization_resolves_from_preferences():
    field = ApplicationField(
        key="job_application[authorized_to_work_in_brazil]",
        label="Are you legally authorized to work in Brazil?",
        field_type="radio",
        semantic_type="work_authorization",
        options=["Yes", "No"],
        confidence=1.0,
    )
    resolution = _resolver(preferences=CandidatePreferences(work_authorization=["Brazil"])).resolve_field(field)

    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.answer == "Yes"
    assert resolution.supported_by == ("CandidatePreferences.work_authorization",)


def test_qa_04_an_exact_approved_answer_wins():
    field = ApplicationField(key="q-salary", label="Salary expectation")
    resolution = _resolver(answers=_approved("Salary expectation", "USD 3000 per month")).resolve_field(field)

    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.answer == "USD 3000 per month"
    assert resolution.source == "approved_answer"
    assert "fact" not in resolution.source


def test_qa_05_a_semantically_equivalent_question_reuses_the_approved_answer():
    """Mesma pergunta com outra redacao: aprovada uma vez, vale depois."""
    knowledge = _approved("Why do you want to work with WordPress?", "I have built WordPress products since 2017.")
    # Redacao proxima, nao identica: e o caso que exige equivalencia semantica.
    field = ApplicationField(key="q-why", label="Why do you want to work at WordPress")
    resolution = _resolver(answers=knowledge).resolve_field(field)

    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.source == "approved_semantic"
    assert resolution.confidence >= 0.88
    assert resolution.answer == "I have built WordPress products since 2017."
    assert resolution.supported_by


# --- QA-06..QA-07, QA-11: geracao grounded e validada --------------------------


def test_qa_06_an_open_question_is_generated_from_authorized_context():
    field = ApplicationField(
        key="job_application[why_this_role]",
        label="Why are you interested in this role?",
        field_type="textarea",
    )
    resolution = _resolver(provider=GroundedTemplateProvider()).resolve_field(field)

    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.generated is True
    assert resolution.source == "generated_grounded"
    assert resolution.answer.strip()
    assert resolution.supported_by, "resposta gerada sem proveniencia nao serve"
    assert all(item.startswith(("profile.", "preferences.", "resume.")) for item in resolution.supported_by)


def test_qa_07_an_invented_metric_is_rejected():
    """O modelo escreveu; o validador nao acreditou."""

    class InventiveProvider:
        def generate(self, question, *, job_context, candidate_context, constraints):
            return GeneratedAnswer(
                "I increased WordPress performance by 300% at Google.",
                ("profile.skills.wordpress",),
                0.99,
            )

    field = ApplicationField(key="q-open", label="Describe your WordPress experience.", field_type="textarea")
    resolution = _resolver(provider=InventiveProvider()).resolve_field(field)

    assert resolution.status == ResolutionStatus.NEEDS_HUMAN.value
    assert resolution.answer == ""
    assert "generated_claim_unsupported" in resolution.reason
    assert "UNSUPPORTED_NUMBER" in resolution.reason
    assert "UNSUPPORTED_ENTITY" in resolution.reason


def test_qa_11_a_generated_answer_without_support_is_rejected():
    class UnsupportedProvider:
        def generate(self, question, *, job_context, candidate_context, constraints):
            return GeneratedAnswer("I am a great fit for this role.", (), 0.9)

    field = ApplicationField(key="q-open", label="Why are you a good fit?", field_type="textarea")
    resolution = _resolver(provider=UnsupportedProvider()).resolve_field(field)

    assert resolution.status == ResolutionStatus.NEEDS_HUMAN.value
    assert "GENERATED_ANSWER_WITHOUT_SUPPORT" in resolution.reason


def test_qa_11b_a_generator_that_lies_about_provenance_is_rejected():
    """`supported_by` apontando para contexto inexistente tambem e recusado."""

    class LyingProvider:
        def generate(self, question, *, job_context, candidate_context, constraints):
            return GeneratedAnswer("I have 7 years of WordPress experience.", ("fact:invented",), 0.9)

    field = ApplicationField(key="q-open", label="Tell us about your WordPress work.", field_type="textarea")
    resolution = _resolver(provider=LyingProvider()).resolve_field(field)

    assert resolution.status == ResolutionStatus.NEEDS_HUMAN.value
    assert "SUPPORT_NOT_IN_CONTEXT" in resolution.reason


# --- QA-08..QA-10: o que NAO se responde --------------------------------------


def test_qa_08_a_factual_question_without_data_needs_a_human():
    field = ApplicationField(key="q-salary", label="Expected salary")
    resolution = _resolver().resolve_field(field)

    assert resolution.status == ResolutionStatus.NEEDS_HUMAN.value
    assert resolution.requires_human is True
    assert resolution.reason == "no_verifiable_fact"
    assert resolution.to_answer(field) is None


def test_qa_08b_a_factual_question_is_never_answered_by_the_generator():
    """Mesmo com gerador disponivel, fato desconhecido nao vira texto."""

    class AlwaysAnswers:
        def generate(self, question, *, job_context, candidate_context, constraints):
            return GeneratedAnswer("Probably around 9000.", (), 0.5)

    field = ApplicationField(key="q-salary", label="Expected salary")
    resolution = _resolver(provider=AlwaysAnswers()).resolve_field(field)

    assert resolution.status == ResolutionStatus.NEEDS_HUMAN.value
    assert resolution.reason == "no_verifiable_fact"


@pytest.mark.parametrize(
    "label",
    [
        "Do you have a disability?",
        "What is your race/ethnicity?",
        "Are you a protected veteran?",
        "Have you ever been convicted of a crime?",
        "Do you now or in the future require visa sponsorship?",
    ],
)
def test_qa_09_legal_and_self_identification_never_generate(label: str):
    field = ApplicationField(key="q-legal", label=label, field_type="select", options=["Yes", "No"])
    resolution = _resolver(provider=GroundedTemplateProvider()).resolve_field(field)

    assert resolution.status == ResolutionStatus.NEEDS_HUMAN.value
    assert resolution.reason == "sensitive_without_explicit_fact"
    assert resolution.generated is False


def test_qa_09b_an_explicit_preference_still_answers_a_sensitive_question():
    field = ApplicationField(
        key="job_application[requires_sponsorship]",
        label="Will you require sponsorship?",
        field_type="radio",
        semantic_type="requires_sponsorship",
        options=["Yes", "No"],
        confidence=1.0,
    )
    resolution = _resolver(preferences=CandidatePreferences(requires_sponsorship="no")).resolve_field(field)

    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.answer == "No"


def test_qa_10_a_value_outside_the_options_is_refused():
    """Resposta que o formulario nao consegue receber nao e resposta."""
    field = ApplicationField(
        key="q-select",
        label="Which office do you prefer?",
        field_type="select",
        options=["Lisbon", "Porto"],
    )
    resolution = _resolver(answers=_approved("Which office do you prefer?", "Madrid")).resolve_field(field)

    assert resolution.status == ResolutionStatus.NEEDS_HUMAN.value
    assert resolution.answer == ""


def test_qa_10b_a_boolean_intent_maps_to_an_existing_option():
    from jobsearch_agent.resolver import _map_to_option

    field = ApplicationField(key="q", label="Sponsorship?", field_type="radio", options=["Yes", "No"])
    assert _map_to_option("No sponsorship required", field) == "No"
    assert _map_to_option("Yes, I do require it", field) == "Yes"
    assert _map_to_option("maybe later", field) == "No"


# --- QA-12: idioma -------------------------------------------------------------


def test_qa_12_the_generated_answer_uses_the_job_language():
    field = ApplicationField(key="q-open", label="Por que voce se interessou por esta vaga?", field_type="textarea")
    portuguese = _resolver(provider=GroundedTemplateProvider(), language="pt-BR").resolve_field(field)
    english = _resolver(provider=GroundedTemplateProvider(), language="en-US").resolve_field(field)

    assert portuguese.status == ResolutionStatus.RESOLVED.value
    assert "Tenho interesse" in portuguese.answer
    assert english.status == ResolutionStatus.RESOLVED.value
    assert "I am interested" in english.answer


# --- o contrato ---------------------------------------------------------------


def test_the_resolver_never_returns_bare_none(tmp_path: Path):
    """O defeito que este ticket existe para corrigir: `None` sem explicacao."""
    resolver = _resolver(provider=GroundedTemplateProvider())
    fields = [
        ApplicationField(key="a", label="First name"),
        ApplicationField(key="b", label="How many years of Rust experience do you have?"),
        ApplicationField(key="c", label="Why are you interested in this role?", field_type="textarea"),
        ApplicationField(key="d", label="Do you have a disability?"),
        ApplicationField(key="e", label="Unexpected question?", field_type="text"),
    ]
    for field in fields:
        resolution = resolver.resolve_field(field)
        assert resolution.status in {status.value for status in ResolutionStatus}
        if resolution.status != ResolutionStatus.RESOLVED.value:
            assert resolution.reason, f"{field.label} parou sem motivo"
        assert json.loads(json.dumps(resolution.to_dict())) == resolution.to_dict()
        assert isinstance(resolution.supported_by, tuple)


def test_the_context_builder_gives_every_item_an_origin():
    context = CandidateContextBuilder(_profile(), CandidatePreferences(), job=_job()).build()
    assert context
    for item in context:
        assert item.key and item.text
    identity = next(item for item in context if item.semantic_type == "first_name")
    assert "fact_qa_first_name" in identity.text
    skills = [item for item in context if item.kind == "skill"]
    assert any(item.value == "7" for item in skills)


def test_the_fact_validator_accepts_a_grounded_answer():
    context = CandidateContextBuilder(_profile(), CandidatePreferences(), job=_job()).build()
    validation = FactValidator().validate(
        "I have 7 years of WordPress experience at Example Studio.",
        supported_by=("profile.skills.wordpress", "profile.experiences.exp-qa-1"),
        context=context,
        allowed_topic_text="role: WordPress Developer\ncompany: Acme",
    )
    assert validation.valid is True


def test_the_fact_validator_rejects_an_invented_salary():
    context = CandidateContextBuilder(_profile(), CandidatePreferences(), job=_job()).build()
    validation = FactValidator().validate(
        "I expect USD 250000 per year.",
        supported_by=("profile.skills.wordpress",),
        context=context,
    )
    assert validation.valid is False
    assert any(error.startswith("UNSUPPORTED_SALARY") for error in validation.errors)


def test_the_fact_validator_rejects_an_invented_certification():
    context = CandidateContextBuilder(_profile(), CandidatePreferences(), job=_job()).build()
    validation = FactValidator().validate(
        "I am an AWS Certified Solutions Architect.",
        supported_by=("profile.skills.wordpress",),
        context=context,
    )
    assert validation.valid is False
    assert any("AWS" in error or "CERTIFIED" in error.upper() for error in validation.errors)


def test_generation_constraints_reach_the_provider():
    seen: dict[str, object] = {}

    class SpyProvider:
        def generate(self, question, *, job_context, candidate_context, constraints):
            seen["constraints"] = constraints
            seen["job_context"] = job_context
            seen["items"] = len(candidate_context)
            return GeneratedAnswer("Grounded sentence.", ("profile.skills.wordpress",), 0.9)

    field = ApplicationField(
        key="q-open",
        label="Why are you interested in this role?",
        field_type="textarea",
        semantic_context={"max_length": 240},
    )
    resolution = _resolver(provider=SpyProvider()).resolve_field(field)

    assert resolution.status == ResolutionStatus.RESOLVED.value
    constraints = seen["constraints"]
    assert isinstance(constraints, GenerationConstraints)
    assert constraints.max_length == 240
    assert constraints.language == "en-US"
    assert "WordPress Developer" in str(seen["job_context"])
    assert int(seen["items"]) > 0


def test_a_generated_answer_longer_than_the_field_is_refused():
    class VerboseProvider:
        def generate(self, question, *, job_context, candidate_context, constraints):
            return GeneratedAnswer("Word " * 100, ("profile.skills.wordpress",), 0.9)

    field = ApplicationField(
        key="q-open",
        label="Why are you interested in this role?",
        field_type="textarea",
        semantic_context={"max_length": 40},
    )
    resolution = _resolver(provider=VerboseProvider()).resolve_field(field)

    assert resolution.status == ResolutionStatus.NEEDS_HUMAN.value
    assert resolution.reason == "generated_answer_too_long"


def test_qa_08c_the_real_greenhouse_location_label_resolves_from_the_profile():
    """`Location (City) *` e o rotulo REAL do Greenhouse.

    Sem remover pontuacao na normalizacao, o cue "location city" nao casava com
    "location (city) *" e um fato presente no perfil virava pergunta sem resposta.
    Achado na execucao contra a vaga da Fueled (PROVIDER-CERT-001).
    """
    field = ApplicationField(key="candidate-location", label="Location (City) *", field_type="combobox")
    resolution = _resolver().resolve_field(field)

    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.answer == "São Paulo, Brazil"
    assert resolution.supported_by
    assert resolution.generated is False


def test_the_option_matching_ignores_punctuation_and_case():
    from jobsearch_agent.resolver import _map_to_option, _option_mismatch

    field = ApplicationField(
        key="q-term",
        label="Employment type",
        field_type="checkbox",
        options=["Full-Time", "Contract/Freelance"],
    )
    assert _option_mismatch("full time", field) is False
    assert _map_to_option("FULL-TIME", field) == "Full-Time"


# --- CERT-001A: capacidades deterministicas observadas na vaga real da Fueled ---


def test_cert_a02_country_derives_the_region_option():
    """Pais no perfil -> opcao de regiao do ATS. Derivacao geografica, nao LLM."""
    from jobsearch_agent.resolver import _region_for, _map_region_option
    from jobsearch_agent.models import ApplicationField

    profile = _profile()
    profile.identity["country"] = "Brazil"
    assert _region_for(profile) == "latin america"

    field = ApplicationField(
        key="q-region",
        label="Please select the region where you currently live: *",
        field_type="select",
        options=["North America", "Latin America", "Europe", "Asia"],
    )
    assert _map_region_option("latin america", field) == "Latin America"

    # Sem casamento seguro com as opcoes, a resposta e humana.
    other = ApplicationField(key="q-region", label="Region", field_type="select", options=["APAC", "Europe"])
    assert _map_region_option("latin america", other) is None


def test_cert_a02b_an_unknown_country_does_not_produce_a_region():
    from jobsearch_agent.resolver import _region_for

    profile = _profile()
    profile.identity["country"] = "Atlantis"
    profile.identity.pop("current_location", None)
    assert _region_for(profile) == ""


def test_cert_a03_a_self_sourced_answer_makes_the_detail_field_not_applicable():
    """A pergunta condicional da Fueled: origem propria -> "N/A"."""
    resolver = _resolver()
    field = ApplicationField(
        key="question_details",
        label="Please provide additional details if you selected Employee Referral, Job Board, or Other (N/A if not applicable). *",
        field_type="text",
    )
    resolution = resolver.resolve_field(field, siblings={"question_source": "LinkedIn"})

    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.answer == "N/A"
    assert resolution.generated is False
    assert "llm" not in resolution.source.casefold()


@pytest.mark.parametrize("source", ["Employee Referral", "Other", "Job Board"])
def test_cert_a03b_a_source_that_requires_detail_stays_human(source: str):
    field = ApplicationField(
        key="question_details",
        label="Please provide additional details if you selected Employee Referral, Job Board, or Other (N/A if not applicable). *",
        field_type="text",
    )
    resolution = _resolver().resolve_field(field, siblings={"question_source": source})

    assert resolution.status == ResolutionStatus.NEEDS_HUMAN.value
    assert resolution.answer == ""


def test_cert_a03c_without_the_conditioning_answer_nothing_is_invented():
    field = ApplicationField(
        key="question_details",
        label="Please provide additional details if you selected Employee Referral, Job Board, or Other. *",
        field_type="text",
    )
    resolution = _resolver().resolve_field(field, siblings={})

    assert resolution.status == ResolutionStatus.NEEDS_HUMAN.value


def test_cert_a04_employment_types_map_to_the_exact_option():
    """Dois tipos aceitos -> a opcao que cobre os DOIS, e nao a primeira que casa."""
    field = ApplicationField(
        key="question_engagement[]",
        label="Full-Time",
        field_type="checkbox",
        options=["Full-Time", "Contract/Freelance", "Open to Contract and Full-Time Opportunities"],
    )
    both = _resolver(preferences=CandidatePreferences(employment_types=["full_time", "contract"]))
    assert both.resolve_field(field).answer == "Open to Contract and Full-Time Opportunities"

    only_full = _resolver(preferences=CandidatePreferences(employment_types=["full_time"]))
    assert only_full.resolve_field(field).answer == "Full-Time"

    only_contract = _resolver(preferences=CandidatePreferences(employment_types=["contract"]))
    assert only_contract.resolve_field(field).answer == "Contract/Freelance"

    unknown = _resolver(preferences=CandidatePreferences())
    assert unknown.resolve_field(field).status == ResolutionStatus.NEEDS_HUMAN.value


def test_cert_a05_notice_period_preference_maps_to_the_ats_option():
    field = ApplicationField(
        key="question_notice",
        label="If offered the role, how much notice do you need to provide before you can start? *",
        field_type="select",
        options=["Immediately", "2 weeks", "30 days", "Other"],
    )
    immediate = _resolver(preferences=CandidatePreferences(notice_period="immediate"))
    resolution = immediate.resolve_field(field)
    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.answer == "Immediately"
    assert resolution.supported_by == ("preferences.notice_period",)

    thirty = _resolver(preferences=CandidatePreferences(notice_period="30_days"))
    assert thirty.resolve_field(field).answer == "30 days"

    # Preferencia ausente: nao se assume "imediato".
    assert _resolver().resolve_field(field).status == ResolutionStatus.NEEDS_HUMAN.value


def test_cert_a06_an_attached_file_is_not_a_question_for_a_human():
    """O curriculo anexado aparecia como NEEDS_HUMAN na auditoria — era ruido."""
    attached = ApplicationField(
        key="resume", label="Attach", field_type="file", semantic_type="resume",
        required=True, attachment_path="/tmp/resume.pdf",
    )
    resolution = _resolver().resolve_field(attached)
    assert resolution.status == ResolutionStatus.UNSUPPORTED.value
    assert resolution.reason == "artifact_attached"
    assert resolution.requires_human is False

    missing = ApplicationField(key="resume", label="Attach", field_type="file", semantic_type="resume", required=True)
    assert _resolver().resolve_field(missing).reason == "artifact_required"


def test_cert_a08_pronouns_are_never_inferred():
    """Sem declaracao explicita, pronouns continua humano — nada de inferir."""
    field = ApplicationField(key="q-pronouns", label="What are your pronouns? *", field_type="text")
    assert _resolver().resolve_field(field).status == ResolutionStatus.NEEDS_HUMAN.value

    from jobsearch_agent.models import CareerProfile, Experience
    declared = CareerProfile(
        identity={"pronouns": "ele/dele (he/him)"},
        professional_summary={},
        experiences=[],
        skills={},
        languages={},
        demo=False,
    )
    resolver = QuestionResolver(
        knowledge=AnswerKnowledgeBase([]), profile=declared, preferences=CandidatePreferences(), job=_job()
    )
    resolution = resolver.resolve_field(field)
    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.answer == "ele/dele (he/him)"
    assert resolution.supported_by == ("profile.identity.pronouns",)


def test_cert_a09_none_of_these_paths_generate_text():
    """Todas as capacidades do CERT-001A sao deterministicas."""
    class ExplodingProvider:
        def generate(self, *args, **kwargs):
            raise AssertionError("nenhum destes caminhos pode usar geracao")

    resolver = QuestionResolver(
        knowledge=AnswerKnowledgeBase([]),
        profile=_profile(),
        preferences=CandidatePreferences(
            notice_period="immediate", employment_types=["full_time", "contract"]
        ),
        job=_job(),
        provider=ExplodingProvider(),
    )
    profile = _profile()
    profile.identity["country"] = "Brazil"
    resolver.context_builder.profile = profile
    resolver.profile = profile
    resolver.profile.identity["country"] = "Brazil"

    fields = [
        ApplicationField(key="candidate-location", label="Location (City) *", field_type="combobox"),
        ApplicationField(key="q-region", label="Region where you currently live", field_type="select", options=["Latin America", "Europe"]),
        ApplicationField(key="q-engagement[]", label="Employment type", field_type="checkbox", options=["Full-Time", "Contract/Freelance", "Open to Contract and Full-Time Opportunities"]),
        ApplicationField(key="q-notice", label="Notice period", field_type="select", options=["Immediately", "30 days"]),
        ApplicationField(key="q-details", label="Details if you selected Other", field_type="text"),
        ApplicationField(key="resume", label="Attach", field_type="file", attachment_path="/tmp/r.pdf"),
    ]
    for field in fields:
        resolution = resolver.resolve_field(field, siblings={"question_source": "LinkedIn"})
        assert resolution.status != ResolutionStatus.NEEDS_HUMAN.value or field.key == "q-details", field.key
        assert resolution.generated is False, field.key


def test_cert_a03d_a_custom_question_is_found_by_its_label_not_its_key():
    """Na Greenhouse a chave e opaca: `question_18722965008`.

    O campo condicional depende de "How did you hear about this opportunity?", e
    procurar o irmao pela CHAVE nao encontrava nada — a resposta existia e o campo
    ficava humano.
    """
    from jobsearch_agent.resolver import normalize_label

    # O indice de irmaos e montado pelo orquestrador com chave E rotulo.
    siblings = {
        "question_18722965008": "Linkedin",
        normalize_label("How did you hear about this opportunity? *"): "Linkedin",
    }
    field = ApplicationField(
        key="question_18722966008",
        label="Please provide additional details if you selected Employee Referral, Job Board, or Other (N/A if not applicable). *",
        field_type="text",
    )
    resolution = _resolver().resolve_field(field, siblings=siblings)

    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.answer == "N/A"
    assert resolution.source == "conditional:self_sourced"


def test_cert_a05b_a_combobox_without_options_still_answers_a_declared_preference():
    """A Greenhouse carrega as opcoes por typeahead: `options` chega vazio.

    O valor vem da preferencia declarada; quem casa com a opcao e o widget. Se ele
    nao casar, o preenchimento falha — e nao grava outra coisa.
    """
    field = ApplicationField(
        key="question_18722973008",
        label="If offered the role, how much notice do you need to provide before you can start? *",
        field_type="combobox",
    )
    resolution = _resolver(preferences=CandidatePreferences(notice_period="immediate")).resolve_field(field)

    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.answer == "Immediately"
    assert resolution.supported_by == ("preferences.notice_period",)


def test_cert_a05c_sponsorship_outside_the_us_is_not_a_plain_no():
    """A pergunta e sobre trabalhar NOS EUA; "No" ali afirma work authorization.

    Achado real: o formulario da Fueled pergunta "will you require employer
    sponsorship ... in the U.S.?" e a opcao "No" do board e "No - I am authorized
    to work in the U.S. and will not require sponsorship". O candidato mora no
    Brasil e nunca declarou isso; a resposta correta e a que descreve a situacao
    dele, derivada do pais.
    """
    residency = _profile()
    residency.identity["country"] = "Brazil"
    policy = AnswerKnowledgeBase(
        [], rules=[AnswerRule(name="sponsorship", match=("sponsorship",), source="requires_sponsorship")]
    )
    field = ApplicationField(
        key="question_18722972008",
        label="Will you require employer sponsorship or immigration support to work for Fueled in the U.S., either now or in the future? *",
        field_type="combobox",
        required=True,
    )
    resolution = _resolver(
        answers=policy,
        preferences=CandidatePreferences(requires_sponsorship="no"),
        identity_profile=residency,
    ).resolve_field(field)

    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.answer == "Not applicable - I am located outside of the U.S."
    assert "profile.identity.country" in resolution.supported_by


def test_cert_a05d_a_us_candidate_still_answers_the_plain_no():
    residency = _profile()
    residency.identity["country"] = "United States"
    residency.identity["current_location"] = "Austin, United States"
    policy = AnswerKnowledgeBase(
        [], rules=[AnswerRule(name="sponsorship", match=("sponsorship",), source="requires_sponsorship")]
    )
    field = ApplicationField(
        key="q-sponsorship",
        label="Will you require employer sponsorship or immigration support to work in the U.S.? *",
        field_type="combobox",
        required=True,
    )
    resolution = _resolver(
        answers=policy,
        preferences=CandidatePreferences(requires_sponsorship="no"),
        identity_profile=residency,
    ).resolve_field(field)

    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.answer == "No"


def test_cert_a06b_a_closed_widget_never_receives_a_written_answer():
    """Um combobox so aceita as opcoes do board: redacao nao e uma delas.

    Na vaga real da Fueled, "Which of the following best describes your
    experience ...?" (combobox) recebeu um paragrafo de motivacao gerado. O
    widget nao tinha o que escolher, o campo obrigatorio ficaria vazio e a
    telemetria dizia "respondida".
    """
    field = ApplicationField(
        key="question_18722968008",
        label="Which of the following best describes your experience working in a digital agency or consulting firm? *",
        field_type="combobox",
        required=True,
    )
    resolution = _resolver(provider=GroundedTemplateProvider()).resolve_field(field)

    assert resolution.status == ResolutionStatus.NEEDS_HUMAN.value
    assert resolution.reason == "closed_option_without_declared_answer"


def test_cert_a06c_an_open_text_question_still_accepts_a_grounded_answer():
    field = ApplicationField(
        key="q-motivation",
        label="Why do you want to work here?",
        field_type="textarea",
        required=True,
    )
    resolution = _resolver(provider=GroundedTemplateProvider()).resolve_field(field)

    assert resolution.status == ResolutionStatus.RESOLVED.value
    assert resolution.source == "generated_grounded"


def test_cert_a02b_notice_period_aliases_cover_the_board_wording():
    """A opcao real do board e "Available immediately"."""
    from jobsearch_agent.options import option_candidates

    candidates = option_candidates(semantic_type="notice_period", value="Immediately")

    assert "available immediately" in candidates
    assert candidates[0] == "Immediately"
