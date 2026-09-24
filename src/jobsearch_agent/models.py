"""Contratos de domínio serializáveis e independentes de Hermes/browser."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
import re
from typing import Any

try:
    from pydantic.dataclasses import dataclass as domain_dataclass
except ImportError:  # pragma: no cover - modo mínimo sem dependências instaladas
    domain_dataclass = dataclass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobState(StrEnum):
    DISCOVERED = "DISCOVERED"
    NORMALIZED = "NORMALIZED"
    ANALYZED = "ANALYZED"
    SCORED = "SCORED"
    QUALIFIED = "QUALIFIED"
    RESUME_STRATEGY_READY = "RESUME_STRATEGY_READY"
    RESUME_READY = "RESUME_READY"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class IdentityStrength(StrEnum):
    STRONG = "strong"
    WEAK = "weak"


class DuplicateStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED_DUPLICATE = "confirmed_duplicate"
    CONFIRMED_DISTINCT = "confirmed_distinct"


class FitCriterionStatus(StrEnum):
    MATCH = "match"
    PARTIAL = "partial"
    MISSING = "missing"
    BLOCKER = "blocker"
    UNKNOWN = "unknown"


@domain_dataclass
class WorkAuthorizationRequirement:
    required: bool = False
    countries: list[str] = field(default_factory=list)
    sponsorship_available: str = "unknown"
    evidence: str = ""
    source: str = "deterministic"


class ApplicationState(StrEnum):
    DRAFT = "DRAFT"
    PREPARING = "PREPARING"
    MATERIALS_READY = "MATERIALS_READY"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    READY_TO_APPLY = "READY_TO_APPLY"
    REVIEW_REACHED = "REVIEW_REACHED"
    SUBMIT_AUTHORIZED = "SUBMIT_AUTHORIZED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    SUBMIT_FAILED = "SUBMIT_FAILED"
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
    NEEDS_ANSWER = "NEEDS_ANSWER"
    NEEDS_ARTIFACT = "NEEDS_ARTIFACT"
    NEEDS_LOGIN = "NEEDS_LOGIN"
    NEEDS_MFA = "NEEDS_MFA"
    NEEDS_CAPTCHA = "NEEDS_CAPTCHA"
    # O provedor RECUSOU uma submissao que chegou a sair, por nao conseguir
    # verificar o navegador (anti-bot). Diferente de NEEDS_CAPTCHA, que e um
    # desafio ainda nao resolvido sem nenhuma escrita: aqui a candidatura foi
    # enviada e rejeitada. O agente nao deve tentar de novo sozinho nem
    # disfarcar sinais de automacao — e handoff humano explicito.
    NEEDS_HUMAN_CAPTCHA = "NEEDS_HUMAN_CAPTCHA"
    # ADR 0005: o humano assumiu o envio. Continua o HANDOFF, nao o fluxo
    # automatico — retomavel apenas no sentido de continuar o handoff.
    HANDOFF_IN_PROGRESS = "HANDOFF_IN_PROGRESS"
    # Existe uma ALEGACAO de envio manual e ainda nao ha evidencia independente.
    # Comporta-se como SUBMIT_UNKNOWN (nunca reenviar sozinho); a diferenca e a
    # origem da incerteza: o agente enviou e nao sabe o resultado, versus o
    # humano diz que enviou sem confirmacao.
    AWAITING_SUBMISSION_CONFIRMATION = "AWAITING_SUBMISSION_CONFIRMATION"
    UNSUPPORTED_FORM = "UNSUPPORTED_FORM"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    REJECTED = "REJECTED"


class ConfirmationSource(StrEnum):
    """Fontes aceitas de evidencia independente (ADR 0005).

    Conjunto fechado: uma fonte nova exige decisao explicita, porque e ela que
    autoriza a transicao para SUBMITTED.
    """

    CONFIRMATION_EMAIL = "confirmation_email"
    PROVIDER_CONFIRMATION_PAGE = "provider_confirmation_page"
    PROVIDER_APPLICATION_STATUS = "provider_application_status"
    EXTERNALLY_VERIFIED_RECORD = "externally_verified_record"


#: O que NUNCA satisfaz a aresta. Nao sao fontes: sao declaracoes do proprio
#: interessado. A lista existe como DADO, e nao apenas como ausencia no enum,
#: para que a recusa possa ser nomeada em teste e em mensagem de erro — `None`
#: explicaria a mesma coisa, mas nao sobreviveria a uma fonte nova adicionada
#: por engano ao conjunto aceito.
REJECTED_EVIDENCE_KINDS = (
    "user_report",
    "manual_checkbox",
    "free_text",
    "handoff_completion",
)

#: Piso de confianca para a evidencia satisfazer a aresta: quem observou precisa
#: afirmar que aquilo e melhor que acaso. Nao e um escore nem uma media — abaixo
#: do piso a evidencia continua sendo REGISTRADA e e recusada, nunca descartada.
CONFIRMATION_CONFIDENCE_FLOOR = 0.5

_EVIDENCE_TOKEN = re.compile(r"[a-z0-9_.:-]{1,64}")


def _observed_at(value: str) -> datetime:
    """Instante ISO-8601 com fuso explicito.

    Sem fuso o instante e ambiguo, e "quando isto foi observado" e justamente o
    que distingue uma confirmacao de uma declaracao atemporal. Um instante no
    futuro, alem de pequena tolerancia de relogio, e recusado: evidencia nao
    pode descrever algo que ainda nao aconteceu.
    """
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("evidence observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("evidence observed_at must carry an explicit timezone offset")
    if parsed > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("evidence cannot be observed in the future")
    return parsed


@dataclass(frozen=True)
class SubmissionConfirmationEvidence:
    """Evidencia independente de que a candidatura foi aceita (ADR 0005).

    O relato do usuario CRIA a incerteza; ele nao pode satisfaze-la. Por isso este
    objeto e o unico caminho para `SUBMITTED` a partir de
    `AWAITING_SUBMISSION_CONFIRMATION`, e a referencia e OPACA: guardamos o
    identificador da evidencia, nunca o conteudo (que traria dado do candidato).

    `provider` diz QUEM observou (o ATS, o provedor de e-mail, o registro
    externo) e `observed_at` diz QUANDO. Sem os dois, a evidencia nao e
    rastreavel, e uma evidencia que nao se pode rastrear nao e evidencia.
    """

    source: ConfirmationSource
    observed_at: str
    reference: str
    provider: str
    confidence: float = 1.0
    #: Por que o observador considerou isto uma confirmacao. Tokens curtos de um
    #: vocabulario do observador (`company_match`, `time_window_match`, ...).
    #: E o que permite auditar o score em vez de aceitar o numero como fato — e o
    #: que se persiste NO LUGAR do conteudo observado.
    signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source, ConfirmationSource):
            raise ValueError(f"unsupported confirmation source: {self.source!r}")
        if not self.reference or not _EVIDENCE_TOKEN.fullmatch(self.reference):
            raise ValueError("evidence reference must be an opaque short token")
        if not self.provider or not _EVIDENCE_TOKEN.fullmatch(self.provider):
            raise ValueError("evidence provider must be a short lowercase token")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("evidence confidence must be between 0 and 1")
        object.__setattr__(self, "signals", tuple(str(signal) for signal in self.signals))
        for signal in self.signals:
            if not _EVIDENCE_TOKEN.fullmatch(signal):
                raise ValueError(f"evidence signal must be a short token, got {signal!r}")
        _observed_at(self.observed_at)

    def safe_view(self) -> dict[str, Any]:
        """Forma imprimivel: a referencia e opaca, o conteudo nunca entra."""
        return {
            "source": self.source.value,
            "observed_at": self.observed_at,
            "reference": self.reference,
            "provider": self.provider,
            "confidence": float(self.confidence),
            "signals": list(self.signals),
        }


@domain_dataclass
class Job:
    id: str
    source: str
    external_id: str
    company: str
    title: str
    description: str
    location: str = ""
    country: str = ""
    remote_type: str = "unknown"
    employment_type: str = "unknown"
    salary: str = ""
    currency: str = ""
    language: str = "unknown"
    url: str = ""
    posted_at: str = ""
    discovered_at: str = field(default_factory=now_iso)
    requirements: list[str] = field(default_factory=list)
    preferred_requirements: list[str] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    state: JobState = JobState.DISCOVERED


@domain_dataclass
class Fact:
    id: str
    type: str
    statements: dict[str, str]
    tags: list[str] = field(default_factory=list)
    source: str = ""
    verified: bool = True


@domain_dataclass
class Experience:
    id: str
    company: str
    role: str
    start_date: str
    end_date: str = ""
    fact_ids: list[str] = field(default_factory=list)


@domain_dataclass
class Education:
    id: str
    institution: str
    credential: dict[str, str]
    field_of_study: dict[str, str]
    start_date: str = ""
    end_date: str = ""
    fact_ids: list[str] = field(default_factory=list)


@domain_dataclass
class CareerProfile:
    identity: dict[str, str]
    professional_summary: dict[str, str]
    experiences: list[Experience]
    skills: dict[str, dict[str, Any]]
    languages: dict[str, dict[str, Any]]
    preferences: dict[str, Any] = field(default_factory=dict)
    identity_fact_ids: dict[str, str] = field(default_factory=dict)
    candidate_preferences: "CandidatePreferences | None" = None
    summary_fact_ids: dict[str, list[str]] = field(default_factory=dict)
    demo: bool = False
    version: str = "1"
    education: list[Education] = field(default_factory=list)


@domain_dataclass
class ProfileReadiness:
    ready: bool
    blockers: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)


@domain_dataclass
class LanguageResult:
    language: str
    locale: str
    country: str
    confidence: float
    method: str


@domain_dataclass
class LanguageRequirement:
    language: str
    minimum_level: str = "intermediate"
    required: bool = True
    evidence: str = ""
    source: str = "deterministic"


@domain_dataclass
class CandidatePreferences:
    remote: bool = False
    allowed_locations: list[str] = field(default_factory=list)
    allowed_countries: list[str] = field(default_factory=list)
    relocation: bool = False
    minimum_salary: str = ""
    currency: str = ""
    employment_types: list[str] = field(default_factory=list)
    #: Aviso previo para inicio. Valores CANONICOS (`immediate`, `1_week`,
    #: `2_weeks`, `30_days`, `other`); quem traduz para a opcao exata do ATS e o
    #: resolvedor. Vazio significa "nao sei", e nao "imediato".
    notice_period: str = ""
    work_authorization: list[str] = field(default_factory=list)
    requires_sponsorship: str = "unknown"
    timezone: str = ""
    timezones: list[str] = field(default_factory=list)
    max_applications_per_day: int | None = None
    resume_template: str = "ats"
    max_pages: int = 2
    autonomy: dict[str, str] = field(default_factory=dict)


@domain_dataclass
class JobAnalysis:
    language: LanguageResult
    required_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    years_of_experience: str = ""
    education: list[str] = field(default_factory=list)
    language_requirements: list[LanguageRequirement] = field(default_factory=list)
    location_requirements: list[str] = field(default_factory=list)
    work_authorization: str = "unknown"
    work_authorization_requirement: WorkAuthorizationRequirement = field(default_factory=WorkAuthorizationRequirement)
    employment_type: str = "unknown"
    technologies: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    seniority: str = "unknown"
    salary: str = ""
    domain: str = ""
    explanation: list[str] = field(default_factory=list)


@domain_dataclass
class FitResult:
    score: float
    required_match: float
    preferred_match: float
    experience_match: float
    language_match: float
    location_match: float
    matched_skills: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    missing_preferred: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)
    criteria: list["FitCriterionResult"] = field(default_factory=list)


@domain_dataclass
class FitCriterionResult:
    criterion: str
    candidate_value: Any = ""
    requirement: Any = ""
    result: FitCriterionStatus = FitCriterionStatus.UNKNOWN
    score: float = 0.0
    blocker: bool = False
    evidence: str = ""
    source: str = ""


@domain_dataclass
class ResumeStrategy:
    target_role: str
    language: str
    positioning: str
    focus: list[str]
    secondary: list[str]
    deprioritize: list[str]
    keywords: list[str]
    max_pages: int = 2
    template: str = "ats"


@domain_dataclass
class ResumeClaim:
    claim: str
    supported_by: list[str]
    valid: bool = True


@domain_dataclass
class Resume:
    id: str
    job_id: str
    language: str
    header: dict[str, str]
    summary: str
    skills: list[str]
    experience: list[dict[str, Any]]
    education: list[dict[str, Any]] = field(default_factory=list)
    claims: list[ResumeClaim] = field(default_factory=list)
    template: str = "ats"


@domain_dataclass
class ValidationResult:
    valid: bool
    code: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@domain_dataclass
class Application:
    id: str
    job_id: str
    state: ApplicationState = ApplicationState.DRAFT
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


@domain_dataclass
class ApplicationEvent:
    application_id: str
    from_state: ApplicationState
    to_state: ApplicationState
    event: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)


@domain_dataclass
class SubmissionIntent:
    id: str
    application_id: str
    job_id: str
    provider: str
    destination: str
    form_fingerprint: str
    resume_sha256: str
    answers_fingerprint: str
    created_at: str = field(default_factory=now_iso)
    expires_at: str = ""
    status: str = "CREATED"
    authorized_at: str = ""


@domain_dataclass
class ReviewSnapshot:
    application_id: str
    job_id: str
    company: str
    title: str
    provider: str
    destination: str
    resume_filename: str
    resume_sha256: str
    form_fingerprint: str = ""
    answers_fingerprint: str = ""
    resolved_fields: list[dict[str, Any]] = field(default_factory=list)
    manual_questions: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)


@domain_dataclass
class SubmissionAttempt:
    id: str
    intent_id: str
    application_id: str
    provider: str
    method: str
    origin: str
    path_hash: str
    status: str = "SUBMITTING"
    started_at: str = field(default_factory=now_iso)
    completed_at: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@domain_dataclass
class ApplicationAnswer:
    question_key: str
    question: str
    answer: str = ""
    supported_by: list[str] = field(default_factory=list)
    source: str = "unknown"
    confidence: float = 0.0
    approved: bool = False
    legal: bool = False
    semantic_type: str = "unknown"
    field_key: str = ""


@domain_dataclass
class ApplicationReadiness:
    decision: ApplicationState
    ready_to_apply: bool
    requires_review: bool = False
    checks: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


@domain_dataclass
class ApplicationPolicy:
    autonomy: dict[str, str] = field(default_factory=lambda: {
        "search": "auto",
        "analyze": "auto",
        "generate_resume": "auto",
        "answer_known_questions": "auto",
        "fill_forms": "review",
        "submit": "manual",
    })
    providers: dict[str, dict[str, str]] = field(default_factory=dict)
    applications_per_day: int = 20
    unknown_answer: str = "stop"
    captcha: str = "stop"
    mfa: str = "stop"
    legal_question: str = "stop"


@domain_dataclass
class ApplicationField:
    key: str
    label: str
    field_type: str = "text"
    semantic_type: str = "unknown"
    required: bool = False
    options: list[str] = field(default_factory=list)
    value: Any = ""
    answer: ApplicationAnswer | None = None
    confidence: float = 0.0
    source: str = "unknown"
    step: str = ""
    attachment_path: str = ""
    accepted_types: list[str] = field(default_factory=list)
    multiple: bool = False
    disabled: bool = False
    semantic_context: dict[str, Any] = field(default_factory=dict)


@domain_dataclass
class FormCapabilityIssue:
    code: str
    severity: str = "blocker"
    field_key: str = ""
    evidence: str = ""


@domain_dataclass
class ApplicationForm:
    form_id: str
    provider: str = "generic"
    fields: list[ApplicationField] = field(default_factory=list)
    source: str = "fixture"
    steps: list[str] = field(default_factory=list)
    artifact_root: str = ""
    capability_issues: list[FormCapabilityIssue] = field(default_factory=list)
    #: O que o proprio formulario declara. O Lever publica o destino exato no
    #: `action`; usar isso elimina a suposicao de reconstruir a URL.
    action: str = ""
    method: str = "POST"


@domain_dataclass
class ApplicationContext:
    application_id: str
    job_id: str
    fit: dict[str, Any] = field(default_factory=dict)
    resume: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    answers: list[ApplicationAnswer] = field(default_factory=list)
    form: ApplicationForm | None = None
    policy: ApplicationPolicy = field(default_factory=ApplicationPolicy)


def to_dict(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return {key: to_dict(item) for key, item in value.model_dump(mode="json").items()}
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dict(item) for item in value]
    return value
