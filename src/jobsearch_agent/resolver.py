"""Resolvedor unificado de perguntas (JSA-QA-001).

O `AnswerKnowledgeBase` resolve muito — resposta aprovada, regra, identidade,
experiencia, autorizacao de trabalho, sponsorship — mas termina em `None` quando
encontra uma pergunta valida que ninguem cadastrou. E exatamente nesse ponto que
o produto deixa de cumprir o objetivo: a candidatura para.

Aqui existe UM resolvedor que **sempre devolve uma decisao explicita**:

    RESOLVED      ha resposta, com origem e suporte
    NEEDS_HUMAN   a pergunta e legitima e falta fato para responde-la
    UNSUPPORTED   o formulario pede algo que o agente nao sabe receber

Nunca `None` sem explicar por que.

Tres regras sustentam o desenho:

1. **Determinismo primeiro.** O `AnswerKnowledgeBase` continua sendo a fonte
   principal; geracao so entra quando nenhuma fonte verificavel respondeu.
2. **Nao sabe != inventar.** Pergunta factual sem fato vira `NEEDS_HUMAN`.
   Pergunta discursiva pode ser escrita, mas so a partir de contexto
   AUTORIZADO, e cada claim passa por validacao.
3. **Autodeclaracao e legal nao se inferem.** Sem fato explicito, `NEEDS_HUMAN`
   — autonomia nao significa fabricar resposta sobre a vida do candidato.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence

from .models import (
    ApplicationAnswer,
    ApplicationField,
    CandidatePreferences,
    CareerProfile,
    Job,
    Resume,
    ValidationResult,
    to_dict,
)
from .qa import AnswerKnowledgeBase, is_legal_question, question_key


class ResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    UNSUPPORTED = "UNSUPPORTED"


#: Pais -> regiao. DERIVACAO GEOGRAFICA, e nao inferencia sobre a pessoa: se o
#: perfil declara o pais, dizer que ele fica na America Latina e um fato.
#: Pais desconhecido nao vira regiao: vira NEEDS_HUMAN.
REGION_BY_COUNTRY: dict[str, str] = {
    "brazil": "latin america",
    "brasil": "latin america",
    "argentina": "latin america",
    "chile": "latin america",
    "colombia": "latin america",
    "mexico": "latin america",
    "peru": "latin america",
    "uruguay": "latin america",
    "paraguay": "latin america",
    "bolivia": "latin america",
    "ecuador": "latin america",
    "venezuela": "latin america",
    "costa rica": "latin america",
    "panama": "latin america",
    "guatemala": "latin america",
    "honduras": "latin america",
    "nicaragua": "latin america",
    "el salvador": "latin america",
    "dominican republic": "latin america",
    "cuba": "latin america",
    "united states": "north america",
    "usa": "north america",
    "canada": "north america",
    "portugal": "europe",
    "spain": "europe",
    "united kingdom": "europe",
    "ireland": "europe",
    "germany": "europe",
    "france": "europe",
    "netherlands": "europe",
    "poland": "europe",
    "romania": "europe",
}

#: Como cada regiao aparece escrita nos ATS. O casamento e sempre contra as
#: opcoes REAIS do campo; se nenhuma casar, a resposta e NEEDS_HUMAN.
REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "latin america": ("latin america", "latam", "lat am", "south america", "central america"),
    "north america": ("north america", "united states", "usa", "canada"),
    "europe": ("europe", "emea", "european union"),
}

#: Valores canonicos de `notice_period` -> como aparecem nas opcoes do ATS.
#: O PRIMEIRO termo e a forma de exibicao quando o campo nao tem opcoes no DOM;
#: os demais sao formas equivalentes que o widget pode usar. "available
#: immediately" entrou depois da vaga real da Fueled: a opcao do board e
#: "Available immediately" e "Immediately" nao casa com ela nem por prefixo.
NOTICE_PERIOD_ALIASES: dict[str, tuple[str, ...]] = {
    "immediate": ("immediately", "immediate", "as soon as possible", "asap", "0 days", "no notice", "available immediately"),
    "1_week": ("1 week", "one week", "available in 1 week"),
    "2_weeks": ("2 weeks", "two weeks", "available in 2 weeks"),
    "30_days": ("30 days", "1 month", "one month", "4 weeks"),
    "other": ("other", "negotiable"),
}

#: Valores canonicos de `employment_types` -> como aparecem nas opcoes do ATS.
EMPLOYMENT_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "full_time": ("full time", "fulltime", "permanent", "clt"),
    "contract": ("contract", "contractor", "freelance", "pj"),
    "part_time": ("part time", "parttime"),
    "internship": ("internship", "intern"),
}

#: Rotulos que tornam a pergunta DEPENDENTE de outra resposta do mesmo formulario.
DEPENDENT_CUES = (
    "if you selected",
    "if you indicated",
    "if applicable",
    "if yes",
    "se voce selecionou",
    "se aplicavel",
)

#: Perguntas FACTUAIS: existe um valor objetivo no perfil ou nao existe. Nao
#: recebem "criatividade" — sem fato, param.
FACTUAL_SEMANTICS = frozenset(
    {
        "first_name",
        "last_name",
        "preferred_first_name",
        "full_name",
        "email",
        "phone",
        "country",
        "current_location",
        "timezone",
        "linkedin",
        "github",
        "website",
        "portfolio",
        "current_company",
        "experience_years",
        "salary_expectation",
        "notice_period",
        "relocation",
        "work_authorization",
        "requires_sponsorship",
        "education",
        "resume",
        "cover_letter",
    }
)

#: Perguntas DISCURSIVAS: a resposta e texto proprio do candidato, montado a
#: partir de fatos existentes. Podem ser geradas.
DISCURSIVE_SEMANTICS = frozenset({"open_question", "cover_letter_text", "motivation"})

#: Temas em que gerar e PROIBIDO: sem fato ou preferencia explicita, a resposta e
#: do candidato, nao do modelo.
SENSITIVE_SEMANTICS = frozenset(
    {
        "gender",
        "race_ethnicity",
        "veteran_status",
        "disability",
        "work_authorization",
        "requires_sponsorship",
        "criminal_history",
        "visa_status",
    }
)

_SENSITIVE_PATTERNS = (
    "criminal",
    "convicted",
    "conviction",
    "felony",
    "crime",
    "condenado",
    "condenacao",
    "antecedentes criminais",
    "background check",
    "disability",
    "deficien",
    "race",
    "ethnic",
    "veteran",
    "gender",
    "date of birth",
    "visa",
    "sponsorship",
    "authorized to work",
    "work authorization",
    "antecedente",
    "deficien",
    "raca",
    "etnia",
    "veterano",
    "genero",
    "visto",
    "autorizacao de trabalho",
)

_FACTUAL_PATTERNS = (
    "how many years",
    "years of experience",
    "quantos anos",
    "anos de experiencia",
    "current company",
    "empresa atual",
    "expected salary",
    "salary expectation",
    "pretensao",
    "notice period",
    "availability",
    "when can you start",
    "disponibilidade",
    "are you authorized",
    "do you require sponsorship",
    "authorized to work",
    "where are you based",
    "current location",
    "phone number",
    "e mail",
    "linkedin",
    "github",
)

_DISCURSIVE_PATTERNS = (
    "why are you interested",
    "why do you want",
    "por que voce",
    "por que se interessou",
    "describe",
    "descreva",
    "tell us about",
    "conte sobre",
    "what interests you",
    "why would you be a good fit",
    "why are you a good fit",
    "good fit",
    "por que voce seria",
    "por que voce e um bom",
    "explain how",
    "explique como",
    "share an example",
    "give an example",
    "de um exemplo",
)

_NUMBER_CLAIM = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:\+)?\s*(?:%|years?|anos?|yrs?|months?|meses?|projects?|projetos?|clients?|clientes?|teams?|equipes?)?",
    re.IGNORECASE,
)
_MONEY_CLAIM = re.compile(r"(?:usd|brl|eur|r\$|\$|€)\s?\d[\d.,]*", re.IGNORECASE)
_CAPITALIZED = re.compile(r"\b[A-Z][A-Za-z0-9+#.]{2,}\b")
_SENTENCE_START = re.compile(r"(?:^|[.!?]\s+)([A-Z][A-Za-z0-9+#.]{2,})")

#: Palavras capitalizadas que NAO sao claim (inicio de frase, pronomes, comuns).
_STOPWORD_CAPITALS = frozenset(
    {
        "the", "this", "that", "these", "those", "i", "my", "me", "we", "our", "you", "your",
        "and", "but", "for", "with", "from", "have", "has", "had", "would", "could", "should",
        "there", "their", "they", "it", "as", "in", "on", "at", "to", "of", "a", "an", "is",
        "am", "are", "was", "were", "be", "been", "being", "do", "does", "did", "will", "can",
        "if", "when", "while", "because", "also", "more", "most", "much", "many", "over",
        "using", "used", "use", "work", "working", "role", "team", "company", "position",
        "job", "experience", "years", "year", "skills", "skill", "project", "projects",
        "o", "a", "os", "as", "um", "uma", "de", "do", "da", "dos", "das", "em", "no", "na",
        "e", "ou", "que", "com", "para", "por", "meu", "minha", "sou", "tenho", "trabalho",
        "atuo", "experiencia", "anos", "projetos", "empresa", "vaga", "funcao", "equipe",
    }
)


class QuestionResolverError(ValueError):
    """O resolvedor nao pode decidir. Nenhum campo e preenchido."""


@dataclass(frozen=True)
class QuestionResolution:
    """A DECISAO do motor. `ApplicationAnswer` continua sendo a forma persistida."""

    status: str
    answer: str = ""
    source: str = ""
    confidence: float = 0.0
    supported_by: tuple[str, ...] = ()
    semantic_type: str = ""
    field_key: str = ""
    generated: bool = False
    requires_human: bool = False
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == ResolutionStatus.RESOLVED.value

    def to_answer(self, field: ApplicationField) -> ApplicationAnswer | None:
        if not self.resolved or not self.answer:
            return None
        return ApplicationAnswer(
            question_key=question_key(field.label),
            question=field.label,
            answer=self.answer,
            supported_by=list(self.supported_by),
            source=self.source or "question_resolver",
            confidence=self.confidence,
            approved=True,
            legal=self.semantic_type in {"work_authorization", "requires_sponsorship"},
            semantic_type=self.semantic_type or field.semantic_type,
            field_key=field.key,
        )

    def to_dict(self) -> dict[str, Any]:
        return {key: to_dict(value) for key, value in self.__dict__.items()}


@dataclass(frozen=True)
class CandidateContextItem:
    """Um fato autorizado, com a ORIGEM que o sustenta."""

    key: str
    text: str
    kind: str = "fact"
    semantic_type: str = "unknown"
    value: str = ""


class CandidateContextBuilder:
    """Monta o contexto AUTORIZADO. O modelo pode escrever; nao pode inventar materia-prima."""

    def __init__(
        self,
        profile: CareerProfile,
        preferences: CandidatePreferences | None = None,
        *,
        job: Job | None = None,
        resume: Resume | dict[str, Any] | None = None,
        approved_answers: Sequence[ApplicationAnswer] = (),
    ):
        self.profile = profile
        self.preferences = preferences
        self.job = job
        self.resume = resume
        self.approved_answers = tuple(approved_answers)

    def build(self) -> tuple[CandidateContextItem, ...]:
        items: list[CandidateContextItem] = []
        identity_fact_ids = self.profile.identity_fact_ids or {}
        for key, value in (self.profile.identity or {}).items():
            if not str(value).strip():
                continue
            source = identity_fact_ids.get(key)
            suffix = f" [{source}]" if source else ""
            items.append(
                CandidateContextItem(
                    key=f"profile.identity.{key}",
                    text=f"{key.replace('_', ' ')}: {value}{suffix}",
                    kind="identity",
                    semantic_type=key,
                    value=str(value),
                )
            )
        for language, summary in (self.profile.professional_summary or {}).items():
            if not str(summary).strip():
                continue
            fact_ids = (self.profile.summary_fact_ids or {}).get(language, [])
            suffix = f" [{' '.join(fact_ids)}]" if fact_ids else ""
            items.append(
                CandidateContextItem(
                    key=f"profile.summary.{language}",
                    text=f"{summary}{suffix}",
                    kind="summary",
                    semantic_type="professional_summary",
                )
            )
        for name, skill in (self.profile.skills or {}).items():
            if not isinstance(skill, dict):
                continue
            years = str(skill.get("years", "") or "").strip()
            level = str(skill.get("level", "") or "").strip()
            parts = [name]
            if years:
                parts.append(f"{years} years")
            if level:
                parts.append(level)
            items.append(
                CandidateContextItem(
                    key=f"profile.skills.{name}",
                    text=", ".join(parts),
                    kind="skill",
                    semantic_type="experience_years",
                    value=years,
                )
            )
        for experience in self.profile.experiences or []:
            span = f"{experience.start_date} - {experience.end_date or 'present'}"
            items.append(
                CandidateContextItem(
                    key=f"profile.experiences.{experience.id}",
                    text=f"{experience.role} at {experience.company} ({span})",
                    kind="experience",
                    semantic_type="current_company",
                    value=str(experience.company),
                )
            )
        for education in getattr(self.profile, "education", []) or []:
            credential = education.credential.get("en-US") or education.credential.get("pt-BR") or ""
            items.append(
                CandidateContextItem(
                    key=f"profile.education.{education.id}",
                    text=f"{credential} at {education.institution}".strip(),
                    kind="education",
                    semantic_type="education",
                )
            )
        for language, data in (self.profile.languages or {}).items():
            level = str((data or {}).get("level", "") or "").strip()
            items.append(
                CandidateContextItem(
                    key=f"profile.languages.{language}",
                    text=f"{language}: {level}".strip(": "),
                    kind="language",
                    semantic_type="language",
                )
            )
        preferences = self.preferences
        if preferences is not None:
            if preferences.work_authorization:
                items.append(
                    CandidateContextItem(
                        key="preferences.work_authorization",
                        text="authorized to work in: " + ", ".join(preferences.work_authorization),
                        kind="preference",
                        semantic_type="work_authorization",
                        value=", ".join(preferences.work_authorization),
                    )
                )
            if preferences.requires_sponsorship in {"yes", "no"}:
                items.append(
                    CandidateContextItem(
                        key="preferences.requires_sponsorship",
                        text=f"requires sponsorship: {preferences.requires_sponsorship}",
                        kind="preference",
                        semantic_type="requires_sponsorship",
                        value="yes" if preferences.requires_sponsorship == "yes" else "no",
                    )
                )
            if preferences.relocation:
                items.append(
                    CandidateContextItem(
                        key="preferences.relocation",
                        text="willing to relocate",
                        kind="preference",
                        semantic_type="relocation",
                        value="yes",
                    )
                )
            if preferences.remote:
                items.append(
                    CandidateContextItem(
                        key="preferences.remote",
                        text="available for remote work",
                        kind="preference",
                        semantic_type="remote",
                        value="yes",
                    )
                )
            if preferences.minimum_salary:
                items.append(
                    CandidateContextItem(
                        key="preferences.minimum_salary",
                        text=f"expected compensation: {preferences.minimum_salary} {preferences.currency}".strip(),
                        kind="preference",
                        semantic_type="salary_expectation",
                        value=str(preferences.minimum_salary),
                    )
                )
            if preferences.timezone:
                items.append(
                    CandidateContextItem(
                        key="preferences.timezone",
                        text=f"timezone: {preferences.timezone}",
                        kind="preference",
                        semantic_type="timezone",
                        value=str(preferences.timezone),
                    )
                )
        items.extend(self._resume_items())
        for answer in self.approved_answers:
            if answer.approved and answer.answer:
                items.append(
                    CandidateContextItem(
                        key=f"approved.{answer.question_key}",
                        text=f"{answer.question} -> {answer.answer}",
                        kind="approved_answer",
                        semantic_type=answer.semantic_type,
                        value=answer.answer,
                    )
                )
        return tuple(items)

    def _resume_items(self) -> list[CandidateContextItem]:
        resume = self.resume
        if resume is None:
            return []
        if isinstance(resume, dict):
            summary = str(resume.get("summary", "") or "")
            skills = [str(item) for item in resume.get("skills", []) or []]
            experience = resume.get("experience", []) or []
        else:
            summary = str(resume.summary or "")
            skills = [str(item) for item in resume.skills or []]
            experience = resume.experience or []
        items: list[CandidateContextItem] = []
        if summary:
            items.append(
                CandidateContextItem(key="resume.summary", text=summary, kind="resume", semantic_type="professional_summary")
            )
        if skills:
            items.append(
                CandidateContextItem(key="resume.skills", text="skills: " + ", ".join(skills), kind="resume", semantic_type="skills")
            )
        for index, entry in enumerate(experience):
            if isinstance(entry, dict):
                role = str(entry.get("role", "") or "")
                company = str(entry.get("company", "") or "")
            else:  # pragma: no cover - formato inesperado
                continue
            if role or company:
                items.append(
                    CandidateContextItem(
                        key=f"resume.experience.{index}",
                        text=f"{role} at {company}".strip(),
                        kind="resume",
                        semantic_type="current_company",
                    )
                )
        return items

    # -- consultas -------------------------------------------------------------

    def sources(self) -> set[str]:
        return {item.key for item in self.build()}

    def values(self) -> str:
        return "\n".join(item.text for item in self.build())

    def for_semantic(self, semantic_type: str) -> tuple[CandidateContextItem, ...]:
        return tuple(item for item in self.build() if item.semantic_type == semantic_type)


#: Vocabulario de risco: uma resposta que os mencione precisa de suporte explicito.
_RISK_PATTERNS = (
    "certified",
    "certification",
    "certificado",
    "certificacao",
    "aws",
    "azure",
    "pmp",
    "scrum master",
    "mba",
    "phd",
    "doctorate",
)


class FactValidator:
    """Valida claims de uma resposta GERADA contra o contexto autorizado.

    Nao e uma prova: e um portao. Ele barra as classes que o roadmap nomeia —
    empregador, cargo, anos, certificacao, metrica, tecnologia e salario
    inventados — e exige que `supported_by` aponte para contexto real.
    """

    def validate(
        self,
        answer: str,
        *,
        supported_by: Sequence[str],
        context: Sequence[CandidateContextItem],
        allowed_topic_text: str = "",
    ) -> ValidationResult:
        errors: list[str] = []
        sources = {item.key for item in context}
        if not supported_by:
            errors.append("GENERATED_ANSWER_WITHOUT_SUPPORT")
        unknown = [item for item in supported_by if item not in sources]
        if unknown:
            errors.append("SUPPORT_NOT_IN_CONTEXT:" + ",".join(sorted(unknown)))
        ground = " \n".join([item.text for item in context]) + "\n" + allowed_topic_text
        normalized_ground = _normalize(ground)
        normalized_answer = _normalize(answer)
        lowered_ground = ground.casefold()

        ground_numbers = {match.group(0) for match in re.finditer(r"\d+(?:[.,]\d+)?", ground)}
        for claim in _NUMBER_CLAIM.findall(answer):
            number = re.match(r"\d+(?:[.,]\d+)?", claim)
            if number and number.group(0) not in ground_numbers:
                errors.append(f"UNSUPPORTED_NUMBER:{claim.strip()}")
        for claim in _MONEY_CLAIM.findall(answer):
            digits = re.sub(r"[^\d]", "", claim)
            if digits and digits not in re.sub(r"[^\d]", "", ground):
                errors.append(f"UNSUPPORTED_SALARY:{claim.strip()}")

        sentence_starts = {match.group(1).casefold() for match in _SENTENCE_START.finditer(answer)}
        for token in _CAPITALIZED.findall(answer):
            if token.casefold() in _STOPWORD_CAPITALS or token.casefold() in sentence_starts:
                # Maiuscula no inicio da frase e gramatica, nao claim.
                continue
            if token.casefold() not in lowered_ground:
                errors.append(f"UNSUPPORTED_ENTITY:{token}")
        lowered_answer = answer.casefold()
        for term in _RISK_PATTERNS:
            if term in lowered_answer and term not in lowered_ground:
                errors.append(f"UNSUPPORTED_CREDENTIAL:{term}")
        return ValidationResult(
            not errors,
            "GENERATED_ANSWER_OK" if not errors else "GENERATED_ANSWER_REJECTED",
            errors,
            [],
            {"supported_by": list(supported_by), "claims_checked": True},
        )


@dataclass(frozen=True)
class GeneratedAnswer:
    """Retorno ESTRUTURADO do gerador. Texto sozinho nao serve."""

    answer: str
    supported_by: tuple[str, ...] = ()
    confidence: float = 0.0


@dataclass(frozen=True)
class GenerationConstraints:
    language: str = "en-US"
    kind: str = "discursive"
    max_length: int = 4000
    min_length: int = 1
    options: tuple[str, ...] = ()
    semantic_type: str = ""


class GeneratedAnswerProvider(Protocol):
    """Quem escreve a resposta discursiva. LLM hoje, outro amanha."""

    def generate(
        self,
        question: str,
        *,
        job_context: str,
        candidate_context: Sequence[CandidateContextItem],
        constraints: GenerationConstraints,
    ) -> GeneratedAnswer | None: ...


class GroundedTemplateProvider:
    """Gerador deterministico a partir do contexto autorizado.

    Existe para que o caminho de geracao seja exercitavel sem chave de LLM — e
    para que o produto nao dependa de rede para responder uma pergunta aberta.
    Ele nao inventa nada: toda frase e montada com itens do contexto, e o
    `supported_by` sai desses itens. Um LLM entra no mesmo protocolo
    (`LLMAnswerProvider`) sem mudar nada no resolvedor.
    """

    def generate(
        self,
        question: str,
        *,
        job_context: str,
        candidate_context: Sequence[CandidateContextItem],
        constraints: GenerationConstraints,
    ) -> GeneratedAnswer | None:
        skills = [item for item in candidate_context if item.kind == "skill"]
        experiences = [item for item in candidate_context if item.kind == "experience"]
        summary = [item for item in candidate_context if item.kind == "summary"]
        if not skills and not experiences and not summary:
            return None
        picked: list[CandidateContextItem] = []
        if summary:
            picked.append(summary[0])
        picked.extend(skills[:2])
        picked.extend(experiences[:1])
        if not picked:
            return None
        role = _role_from_job(job_context)
        if constraints.language.casefold().startswith("pt"):
            skill_text = ", ".join(item.text for item in picked)
            answer = (
                f"Tenho interesse nesta posicao porque meu trabalho esta diretamente ligado ao que ela pede. "
                f"Minha experiencia inclui {skill_text}. "
                f"Quero seguir contribuindo nessa area na {role}."
            )
        else:
            focus = ", ".join(item.text for item in skills[:2]) or picked[0].text
            answer = (
                f"I am interested in this role because my day-to-day work is directly related to it. "
                f"My background includes {focus}. "
                f"I want to keep building on that in {role}."
            )
        if not answer.strip():
            return None
        return GeneratedAnswer(answer, tuple_supported(picked), 0.9)


def tuple_supported(items: Sequence[CandidateContextItem]) -> tuple[str, ...]:
    return tuple(item.key for item in items)


def _role_from_job(job_context: str) -> str:
    match = re.search(r"role:\s*([^\n]+)", job_context)
    if match:
        return match.group(1).strip()
    company = re.search(r"company:\s*([^\n]+)", job_context)
    if company:
        return f"this {company.group(1).strip()} team"
    return "this role"


class LLMAnswerProvider:
    """Gerador por LLM com contrato ESTRITO: JSON com answer/supported_by/confidence."""

    def __init__(self, provider: Any, *, max_tokens: int = 700):
        self.provider = provider
        self.max_tokens = max_tokens

    def generate(
        self,
        question: str,
        *,
        job_context: str,
        candidate_context: Sequence[CandidateContextItem],
        constraints: GenerationConstraints,
    ) -> GeneratedAnswer | None:
        from .llm import LLMError, LLMRequest

        sources = "\n".join(f"- {item.key}: {item.text}" for item in candidate_context)
        system = (
            "You write ONE answer for a job application question, in the requested language. "
            "Use ONLY the authorized candidate facts below. Never invent employers, roles, years, "
            "certifications, metrics, technologies or salary. Reply as JSON with exactly the keys "
            '"answer" (string), "supported_by" (array of the source keys you used) and '
            '"confidence" (0..1). If the facts are insufficient, reply {"answer": "", "supported_by": [], "confidence": 0}.'
        )
        user = (
            f"Question: {question}\n"
            f"Language: {constraints.language}\n"
            f"Max length: {constraints.max_length}\n"
            f"Job context:\n{job_context}\n"
            f"Authorized candidate facts:\n{sources}\n"
        )
        try:
            payload = self.provider.complete(LLMRequest(system=system, user=user, schema="answer"))
        except LLMError:
            return None
        if not isinstance(payload, dict):
            return None
        answer = str(payload.get("answer", "") or "").strip()
        if not answer:
            return None
        raw_supported = payload.get("supported_by", []) or []
        if not isinstance(raw_supported, (list, tuple)):
            return None
        try:
            confidence = float(payload.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return GeneratedAnswer(answer, tuple(str(item) for item in raw_supported), confidence)


class QuestionResolver:
    """A decisao unica. O loop e o orquestrador so veem `QuestionResolution`."""

    def __init__(
        self,
        *,
        knowledge: AnswerKnowledgeBase,
        profile: CareerProfile,
        preferences: CandidatePreferences | None = None,
        job: Job | None = None,
        resume: Resume | dict[str, Any] | None = None,
        provider: GeneratedAnswerProvider | None = None,
        validator: FactValidator | None = None,
        context_builder: CandidateContextBuilder | None = None,
    ):
        self.knowledge = knowledge
        self.profile = profile
        self.preferences = preferences
        self.job = job
        self.resume = resume
        self.provider = provider
        self.validator = validator or FactValidator()
        self.context_builder = context_builder or CandidateContextBuilder(
            profile, preferences, job=job, resume=resume, approved_answers=knowledge.answers
        )

    # -- porta unica -----------------------------------------------------------

    def resolve_field(
        self,
        field: ApplicationField,
        *,
        siblings: Mapping[str, str] | None = None,
    ) -> QuestionResolution:
        """Decide UM campo. `siblings` sao as respostas ja resolvidas do formulario.

        Necessario para pergunta DEPENDENTE ("if you selected X, please detail"),
        cuja resposta so existe em funcao de outra.
        """
        semantic_type = str(field.semantic_type or "unknown")

        # 0. Arquivo anexado NAO e pergunta: quem responde e o mecanismo de
        # artefato. Sem isto, o curriculo anexado aparecia como NEEDS_HUMAN na
        # auditoria — ruido que confundia bloqueio real com telemetria.
        if str(field.field_type or "").casefold().strip() == "file":
            attached = bool(str(getattr(field, "attachment_path", "") or "").strip())
            return QuestionResolution(
                status=ResolutionStatus.UNSUPPORTED.value,
                semantic_type=semantic_type,
                field_key=field.key,
                reason="artifact_attached" if attached else "artifact_required",
            )

        # 1..5. Fontes verificaveis: resposta aprovada, regra, perfil,
        # preferencias e equivalencia semantica — na ordem que o
        # `AnswerKnowledgeBase` ja implementa.
        known = self.knowledge.resolve_field(field, self.profile, self.preferences)
        if known is not None and str(known.answer).strip():
            answer = str(known.answer)
            supported_by = tuple(known.supported_by or ())
            # A resposta da politica e sobre o CANDIDATO; a pergunta pode ser
            # sobre a situacao dele (sponsorship nos EUA). Quando sao coisas
            # diferentes, a situacao e derivada de um fato do perfil.
            situation = self._situation_answer(field, supported_by, answer)
            if situation is not None:
                answer, supported_by = situation
            if _option_mismatch(answer, field):
                return self._needs_human(field, "option_mismatch", semantic_type)
            return QuestionResolution(
                status=ResolutionStatus.RESOLVED.value,
                answer=answer,
                source=str(known.source or "knowledge_base"),
                confidence=float(known.confidence or 1.0),
                supported_by=supported_by,
                semantic_type=semantic_type,
                field_key=field.key,
            )

        # 3/4. Fato ESTRUTURADO: quando o adapter nao reconheceu a pergunta, o
        # texto dela ainda pode nomear um fato que existe no perfil. Sem isto,
        # "How many years of WordPress experience?" — pergunta factual, fato
        # presente — cairia em NEEDS_HUMAN.
        structured = self._structured_lookup(field)
        if structured is not None:
            return structured

        # Pergunta dependente: derivada de outra resposta do MESMO formulario.
        dependent = self._dependent_lookup(field, siblings or {})
        if dependent is not None:
            return dependent

        # 5. Equivalencia semantica de resposta JA APROVADA. O
        # `AnswerKnowledgeBase` faz isso, mas so depois do portao de confianca do
        # adapter — e uma pergunta customizada, que o adapter nao reconhece, chega
        # com confianca zero e o reuso nunca acontecia. Reusar material aprovado
        # nao e inferir semantica de campo: a evidencia e a propria resposta.
        reuse = self._approved_semantic(field)
        if reuse is not None:
            return reuse

        # 6. Geracao grounded — so para pergunta DISCURSIVA em campo ABERTO.
        if self._is_sensitive(field, semantic_type):
            return self._needs_human(field, "sensitive_without_explicit_fact", semantic_type)
        # Um combobox/select/radio so aceita as OPCOES do board: um paragrafo
        # gerado nao e uma delas. Na vaga real da Fueled, "Which of the
        # following best describes your experience...?" rendeu uma redacao de
        # motivacao — o widget nao tinha o que escolher e o campo obrigatorio
        # ficaria vazio, com a telemetria dizendo "respondida".
        if _closed_widget(field):
            return self._needs_human(field, "closed_option_without_declared_answer", semantic_type)
        if not self._is_discursive(field, semantic_type):
            return self._needs_human(field, "no_verifiable_fact", semantic_type)
        return self._generate(field, semantic_type)

    # -- situacao do candidato -------------------------------------------------

    def _situation_answer(
        self, field: ApplicationField, supported_by: Sequence[str], answer: str
    ) -> tuple[str, tuple[str, ...]] | None:
        """Reescreve a resposta da POLITICA para a SITUACAO declarada no perfil.

        `requires_sponsorship: no` responde "voce precisa de sponsorship?". O
        formulario da Fueled pergunta isso para trabalhar NOS EUA, e a opcao que
        descreve um candidato que mora fora dos EUA e "Not applicable - I am
        located outside of the U.S.". Manter "No" ali escolheria "No - I am
        authorized to work in the U.S...." — um fato que o candidato nunca
        declarou. A troca e derivada do pais, e o pais entra na evidencia.
        """
        # Import tardio de proposito: `options` reexporta as tabelas daqui, e um
        # import no topo criaria ciclo.
        from .options import option_forms, option_key

        key = option_key(str(field.semantic_type or ""), supported_by)
        if key != "sponsorship":
            return None
        if not _answers_no(answer):
            return None
        if not _us_question(field.label):
            return None
        if _candidate_in_us(self.profile):
            return None
        forms = option_forms(key, "outside_us")
        if not forms:
            return None
        return forms[0], tuple(supported_by) + ("profile.identity.country",)

    # -- reuso aprovado --------------------------------------------------------

    def _approved_semantic(self, field: ApplicationField) -> QuestionResolution | None:
        import difflib

        label = _normalize(field.label)
        if not label:
            return None
        best_ratio, best = 0.0, None
        for answer in self.knowledge.answers:
            candidate = _normalize(answer.question)
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, label, candidate).ratio()
            if ratio > best_ratio:
                best_ratio, best = ratio, answer
        if best is None or best_ratio < SEMANTIC_REUSE_THRESHOLD:
            return None
        if _option_mismatch(best.answer, field):
            return None
        supported = tuple(best.supported_by) or (f"approved.{best.question_key}",)
        return QuestionResolution(
            status=ResolutionStatus.RESOLVED.value,
            answer=str(best.answer),
            source="approved_semantic",
            confidence=round(best_ratio, 4),
            supported_by=supported,
            semantic_type=str(field.semantic_type or "unknown"),
            field_key=field.key,
        )

    # -- dependencia entre campos ---------------------------------------------

    def _dependent_lookup(
        self, field: ApplicationField, siblings: Mapping[str, str]
    ) -> QuestionResolution | None:
        """Resolve campo condicional a partir da resposta que o condiciona.

        O caso real (Fueled): "Please provide additional details if you selected
        Employee Referral, Job Board, or Other (N/A if not applicable)". A resposta
        certa depende de QUAL origem foi escolhida — e, quando a origem e uma opcao
        propria como LinkedIn, a resposta e literalmente "N/A".
        """
        label = _normalize(field.label)
        if not any(_normalize(cue) in label for cue in DEPENDENT_CUES):
            return None
        source_answer = ""
        for key, value in siblings.items():
            if "source" in _normalize(key) or "hear" in _normalize(key):
                source_answer = str(value or "").strip()
                break
        if not source_answer:
            return None
        normalized = _normalize(source_answer)
        for kind, aliases in _SOURCE_PRIORITY:
            if not any(alias in normalized for alias in aliases):
                continue
            if kind == "self_sourced":
                # A origem e um canal proprio: nenhum detalhe adicional se aplica,
                # e o proprio formulario manda responder "N/A".
                return self._resolved(
                    field, "N/A", "conditional:self_sourced",
                    (f"conditional:{_normalize(field.key)}",),
                )
            if kind == "named_board":
                # O detalhe ja foi dado pelo proprio candidato na origem: reusa-la
                # e reuso de resposta, nao invencao.
                return self._resolved(
                    field, source_answer, "conditional:source_answer",
                    (f"conditional:{_normalize(field.key)}",),
                )
            # Referral, Other e "Job Board" generico exigem um detalhe que nao
            # existe em lugar nenhum: para e pede a pessoa.
            return None
        return None

    def _resolved(
        self,
        field: ApplicationField,
        answer: str,
        source: str,
        supported_by: tuple[str, ...],
        semantic_type: str | None = None,
    ) -> QuestionResolution:
        return QuestionResolution(
            status=ResolutionStatus.RESOLVED.value,
            answer=answer,
            source=source,
            confidence=1.0,
            supported_by=supported_by,
            semantic_type=semantic_type or str(field.semantic_type or "unknown"),
            field_key=field.key,
        )

    # -- fato estruturado ------------------------------------------------------

    def _structured_lookup(self, field: ApplicationField) -> QuestionResolution | None:
        """Casa a pergunta com um fato do perfil quando o adapter nao deu semantica."""
        label = _normalize(field.label)
        key = _normalize(field.key)
        haystack = f"{label} {key}"

        if any(cue in haystack for cue in _YEARS_CUES):
            skill = self._skill_named(haystack)
            if skill is not None:
                name, years = skill
                return QuestionResolution(
                    status=ResolutionStatus.RESOLVED.value,
                    answer=years,
                    source="CareerProfile.skills",
                    confidence=1.0,
                    supported_by=(f"profile.skills.{name}",),
                    semantic_type="experience_years",
                    field_key=field.key,
                )

        if any(cue in haystack for cue in _CURRENT_COMPANY_CUES):
            current = _current_experience(self.profile)
            if current is not None and current.company:
                return QuestionResolution(
                    status=ResolutionStatus.RESOLVED.value,
                    answer=str(current.company),
                    source="CareerProfile.experiences",
                    confidence=1.0,
                    supported_by=(f"profile.experiences.{current.id}",),
                    semantic_type="current_company",
                    field_key=field.key,
                )

        if "pronoun" in haystack:
            pronouns = str((self.profile.identity or {}).get("pronouns", "") or "").strip()
            if pronouns:
                return self._resolved(field, pronouns, "CareerProfile.identity", ("profile.identity.pronouns",), "pronouns")

        if any(cue in haystack for cue in _NOTICE_CUES):
            mapped = _map_canonical_option(
                (self.preferences.notice_period if self.preferences else ""), NOTICE_PERIOD_ALIASES, field
            )
            if mapped is not None:
                return self._resolved(
                    field, mapped, "CandidatePreferences.notice_period",
                    ("preferences.notice_period",), "notice_period",
                )

        if any(cue in haystack for cue in _REGION_CUES):
            region = _region_for(self.profile)
            if region:
                mapped = _map_region_option(region, field)
                if mapped is not None:
                    return self._resolved(
                        field, mapped, "CareerProfile.identity.country",
                        (f"profile.identity.{region_source_key(self.profile)}",), "region",
                    )

        if any(cue in haystack for cue in _EMPLOYMENT_CUES):
            mapped = _map_employment_option(self.preferences.employment_types if self.preferences else [], field)
            if mapped is not None:
                return self._resolved(
                    field, mapped, "CandidatePreferences.employment_types",
                    ("preferences.employment_types",), "employment_type",
                )

        for cue, identity_keys, semantic in _IDENTITY_CUES:
            if cue not in haystack:
                continue
            for identity_key in identity_keys:
                value = str((self.profile.identity or {}).get(identity_key, "") or "").strip()
                if not value:
                    continue
                source = (self.profile.identity_fact_ids or {}).get(identity_key)
                supported = (source,) if source else (f"profile.identity.{identity_key}",)
                return QuestionResolution(
                    status=ResolutionStatus.RESOLVED.value,
                    answer=value,
                    source="CareerProfile.identity",
                    confidence=1.0,
                    supported_by=supported,
                    semantic_type=semantic,
                    field_key=field.key,
                )
        return None

    def _skill_named(self, haystack: str) -> tuple[str, str] | None:
        """Habilidade citada na pergunta, com anos declarados no perfil."""
        for name, data in (self.profile.skills or {}).items():
            if not isinstance(data, dict):
                continue
            years = str(data.get("years", "") or "").strip()
            if not years:
                continue
            aliases = {_normalize(name)}
            aliases.update(_normalize(str(tag)) for tag in data.get("tags", []) or [])
            if any(alias and alias in haystack for alias in aliases):
                return str(name), years
        return None

    # -- classificacao ---------------------------------------------------------

    def _is_sensitive(self, field: ApplicationField, semantic_type: str) -> bool:
        if semantic_type in SENSITIVE_SEMANTICS:
            return True
        haystack = _normalize(f"{field.key} {field.label}")
        return any(_normalize(pattern) in haystack for pattern in _SENSITIVE_PATTERNS) or is_legal_question(field.label)

    def _is_discursive(self, field: ApplicationField, semantic_type: str) -> bool:
        if semantic_type in DISCURSIVE_SEMANTICS:
            return True
        if semantic_type in FACTUAL_SEMANTICS:
            return False
        label = _normalize(field.label)
        if any(_normalize(pattern) in label for pattern in _FACTUAL_PATTERNS):
            return False
        if any(_normalize(pattern) in label for pattern in _DISCURSIVE_PATTERNS):
            return True
        # Na duvida, factual: gerar onde faltava fato objetivo e exatamente o
        # erro que este resolvedor existe para impedir.
        return False

    # -- geracao ---------------------------------------------------------------

    def _generate(self, field: ApplicationField, semantic_type: str) -> QuestionResolution:
        if self.provider is None:
            return self._needs_human(field, "no_generator_configured", semantic_type)
        context = self.context_builder.build()
        constraints = GenerationConstraints(
            language=str(getattr(self.job, "language", "") or "en-US"),
            kind="discursive",
            max_length=_max_length(field),
            options=tuple(field.options or ()),
            semantic_type=semantic_type,
        )
        try:
            generated = self.provider.generate(
                field.label,
                job_context=_job_context(self.job),
                candidate_context=context,
                constraints=constraints,
            )
        except Exception as exc:  # gerador instavel nunca derruba a candidatura
            return self._needs_human(field, f"generator_failed:{type(exc).__name__}", semantic_type)
        if generated is None or not str(generated.answer).strip():
            return self._needs_human(field, "generator_returned_nothing", semantic_type)

        validation = self.validator.validate(
            generated.answer,
            supported_by=generated.supported_by,
            context=context,
            allowed_topic_text=_job_context(self.job),
        )
        if not validation.valid:
            return self._needs_human(field, "generated_claim_unsupported:" + ";".join(validation.errors), semantic_type)

        answer = str(generated.answer).strip()
        if _option_mismatch(answer, field):
            mapped = _map_to_option(answer, field)
            if mapped is None:
                return self._needs_human(field, "option_mismatch", semantic_type)
            answer = mapped
        if len(answer) > constraints.max_length:
            return self._needs_human(field, "generated_answer_too_long", semantic_type)
        if len(answer) < constraints.min_length:
            return self._needs_human(field, "generated_answer_too_short", semantic_type)
        return QuestionResolution(
            status=ResolutionStatus.RESOLVED.value,
            answer=answer,
            source="generated_grounded",
            confidence=float(generated.confidence or 0.0),
            supported_by=tuple(generated.supported_by),
            semantic_type=semantic_type,
            field_key=field.key,
            generated=True,
        )

    def _needs_human(self, field: ApplicationField, reason: str, semantic_type: str) -> QuestionResolution:
        return QuestionResolution(
            status=ResolutionStatus.NEEDS_HUMAN.value,
            semantic_type=semantic_type,
            field_key=field.key,
            requires_human=True,
            reason=reason,
        )


#: Cues de "quantos anos": a pergunta e factual e o valor tem de vir do perfil.
_YEARS_CUES = (
    "how many years",
    "years of experience",
    "years experience",
    "years with",
    "quantos anos",
    "anos de experiencia",
    "anos de experiencia com",
)

#: Piso de semelhanca para reusar uma resposta aprovada com outra redacao.
SEMANTIC_REUSE_THRESHOLD = 0.88

_CURRENT_COMPANY_CUES = ("current company", "present employer", "empresa atual", "empregador atual")

#: Perguntas factuais que o adapter pode nao ter reconhecido: o rotulo nomeia o fato.
_IDENTITY_CUES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("where are you based", ("current_location", "location"), "current_location"),
    ("current location", ("current_location", "location"), "current_location"),
    ("location city", ("current_location", "location"), "current_location"),
    ("cidade", ("current_location", "location"), "current_location"),
    ("phone", ("phone", "phone_number"), "phone"),
    ("telefone", ("phone", "phone_number"), "phone"),
    ("e mail", ("email",), "email"),
    ("email", ("email",), "email"),
    ("linkedin", ("linkedin",), "linkedin"),
    ("github", ("github",), "github"),
    ("portfolio", ("website", "portfolio"), "website"),
    ("website", ("website", "portfolio"), "website"),
    ("first name", ("first_name",), "first_name"),
    ("last name", ("last_name",), "last_name"),
    ("full name", ("name", "full_name"), "full_name"),
)


_NOTICE_CUES = ("notice period", "notice", "how much notice", "aviso previo", "quanto tempo de aviso")
_REGION_CUES = ("region", "where you currently live", "where are you based", "regiao", "onde voce mora")
_EMPLOYMENT_CUES = ("employment type", "engagement", "contract type", "full-time", "full time", "tipo de contratacao")

#: Origens, em ORDEM DE PRIORIDADE (a primeira que casa decide). A ordem importa:
#: "Job Board" tambem poderia ser lido como canal proprio, e a resposta certa para
#: ele NAO e "N/A" — e o nome do board.
_SOURCE_PRIORITY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("referral", ("employee referral", "referral", "indicacao", "indicado")),
    ("other", ("other", "outro", "outra")),
    ("named_board", ("indeed", "glassdoor", "wellfound", "angel", "stackoverflow", "weworkremotely", "remote ok", "remoteok", "himalayas")),
    ("generic_board", ("job board", "job site", "job portal", "site de vagas")),
    ("self_sourced", ("linkedin", "company website", "careers page", "site da empresa")),
)


def _region_for(profile: CareerProfile) -> str:
    country = _normalize(str((profile.identity or {}).get("country", "") or ""))
    if country in REGION_BY_COUNTRY:
        return REGION_BY_COUNTRY[country]
    location = _normalize(str((profile.identity or {}).get("current_location", "") or ""))
    for name, region in REGION_BY_COUNTRY.items():
        if name and name in location:
            return region
    return ""


def region_source_key(profile: CareerProfile) -> str:
    identity = profile.identity or {}
    return "country" if str(identity.get("country", "") or "").strip() else "current_location"


def _map_region_option(region: str, field: ApplicationField) -> str | None:
    """Regiao -> opcao EXATA do campo. Sem casamento seguro, `None`."""
    aliases = REGION_ALIASES.get(region, (region,))
    if not field.options:
        # Combobox sem opcoes no DOM: o proprio widget decide o casamento por
        # texto. A regiao e fato derivado do pais, entao nao ha invencao aqui.
        return region.title()
    for option in field.options:
        candidate = _normalize(option)
        if any(alias == candidate or alias in candidate for alias in aliases):
            return option
    return None


def _map_canonical_option(
    canonical: str, aliases: dict[str, tuple[str, ...]], field: ApplicationField
) -> str | None:
    """Valor canonico de preferencia -> opcao exata do campo.

    Convencao: o PRIMEIRO alias e a forma de exibicao. Quando o campo e um
    combobox sem opcoes no DOM (a Greenhouse carrega as opcoes por typeahead), a
    forma de exibicao e a resposta — o widget decide o casamento. O valor vem da
    preferencia declarada, entao nao ha invencao; se o widget nao casar, o
    preenchimento falha em voz alta em vez de gravar algo errado.
    """
    if not canonical:
        return None
    terms = aliases.get(str(canonical).strip().casefold())
    if not terms:
        return None
    if not field.options:
        return terms[0].title()
    for option in field.options:
        candidate = _normalize(option)
        if any(term == candidate or term in candidate for term in terms):
            return option
    return None


def _map_employment_option(employment_types: Sequence[str], field: ApplicationField) -> str | None:
    """Lista canonica de tipos -> UMA opcao exata do campo.

    Com mais de um tipo aceito, a opcao certa e a que cobre os dois
    ("Open to Contract and Full-Time Opportunities"), e nao a primeira que casa.
    """
    wanted = [str(item).strip().casefold() for item in employment_types or [] if str(item).strip()]
    if not wanted or not field.options:
        return None
    matches: list[tuple[str, list[str]]] = []
    for option in field.options:
        candidate = _normalize(option)
        covered = [
            canonical
            for canonical in wanted
            if any(term in candidate for term in EMPLOYMENT_TYPE_ALIASES.get(canonical, (canonical,)))
        ]
        if covered:
            matches.append((option, covered))
    if not matches:
        return None
    if len(wanted) == 1:
        return matches[0][0]
    # Prefere a opcao que cobre TODOS os tipos aceitos; se nenhuma cobrir, nao
    # escolhe por conta propria.
    for option, covered in matches:
        if set(covered) == set(wanted):
            return option
    return None


def _current_experience(profile: CareerProfile):
    open_roles = [item for item in profile.experiences or [] if not item.end_date]
    pool = open_roles or list(profile.experiences or [])
    if not pool:
        return None
    return max(pool, key=lambda item: (item.start_date or "", item.end_date or ""))


def normalize_label(value: str) -> str:
    """Normalizacao publica, para quem precisa indexar por rotulo."""
    return _normalize(value)


def _normalize(value: str) -> str:
    """Normaliza para comparacao: sem acento, sem maiuscula e SEM PONTUACAO.

    A pontuacao importa: o label real do Greenhouse e "Location (City) *", e sem
    remover os parenteses e o asterisco o cue "location city" nao casava — um fato
    que o perfil tinha ficava como pergunta sem resposta. Achado na execucao real
    contra a vaga da Fueled.
    """
    decomposed = unicodedata.normalize("NFKD", str(value).casefold())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


def _job_context(job: Job | None) -> str:
    if job is None:
        return ""
    return (
        f"role: {job.title}\ncompany: {job.company}\nlocation: {job.location}\n"
        f"description: {job.description}"
    )


def _max_length(field: ApplicationField) -> int:
    raw = (field.semantic_context or {}).get("max_length") if field.semantic_context else None
    try:
        value = int(raw) if raw else 0
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else 4000


def _option_mismatch(answer: str, field: ApplicationField) -> bool:
    if not field.options:
        return False
    if field.field_type.casefold() == "checkbox" and field.semantic_type == "checkbox_boolean":
        return False
    normalized = _normalize(answer)
    return all(normalized != _normalize(option) for option in field.options)


def _closed_widget(field: ApplicationField) -> bool:
    """O campo so aceita valores de uma lista fechada (nao aceita redacao).

    `combobox` inclui os typeaheads que carregam as opcoes depois do clique
    (Greenhouse/react-select): continuam sendo lista fechada.
    """
    field_type = str(field.field_type or "").casefold().strip()
    if field_type in {"combobox", "select", "radio", "datalist"}:
        return True
    return field_type == "checkbox" and str(field.semantic_type or "") != "checkbox_boolean"


def _answers_no(answer: str) -> bool:
    return _normalize(answer) in {"no", "nao", "false", "never", "not required"}


#: Como uma pergunta de sponsorship se limita aos EUA.
_US_QUESTION_CUES = ("united states", " u s ", "usa", "u s a", "us work", "in the us")


def _us_question(label: str) -> bool:
    haystack = f" {_normalize(label)} "
    return any(cue in haystack for cue in _US_QUESTION_CUES)


def _candidate_in_us(profile: CareerProfile) -> bool:
    identity = profile.identity or {}
    country = _normalize(str(identity.get("country", "") or ""))
    if not country:
        location = _normalize(str(identity.get("current_location", "") or identity.get("location", "") or ""))
        country = "united states" if "united states" in location or location.endswith(" usa") else ""
    return country in {"united states", "united states of america", "usa", "us"}


def _map_to_option(answer: str, field: ApplicationField) -> str | None:
    """Mapeia a intencao para uma OPCAO existente. Sem casamento seguro, nao mapeia."""
    if not field.options:
        return None
    normalized = _normalize(answer)
    for option in field.options:
        candidate = _normalize(option)
        if candidate == normalized or candidate in normalized or normalized in candidate:
            return option
    wanted_yes = any(term in normalized for term in ("yes", "sim", "required", "authorized"))
    for option in field.options:
        candidate = _normalize(option)
        negative = candidate.startswith("no") or "not " in candidate
        if wanted_yes and candidate.startswith(("yes", "sim")):
            return option
        if not wanted_yes and negative:
            return option
    return None
