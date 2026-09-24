"""Confirmacao observavel da candidatura (JSA-CONF-001..005).

O dominio ja sabe que uma declaracao do usuario nao confirma nada: `SUBMITTED`
exige `SubmissionConfirmationEvidence` de um observador independente. Este modulo
e o lado OBSERVADOR dessa regra — quem procura a evidencia, como ela e pontuada e
como a reconciliacao a submete ao portao do dominio.

O contrato central nao conhece Gmail nem nenhum provedor de e-mail:

    ConfirmationObserver.observe(application, *, since) -> evidencia

Gmail e apenas uma implementacao de `EmailSource`:

    Gmail -> EmailConfirmationObserver -> SubmissionConfirmationEvidence
          -> ApplicationService.confirm_submission()

Tres propriedades sustentam o desenho:

1. **Corroboracao, nao palavra-chave.** A frase de recebimento e PORTA — sem ela
   nao existe candidato — e vale menos que o piso de aceitacao. Uma mensagem so
   com "obrigado por se candidatar" nao confirma nada: precisa de corroboracao
   independente (a origem e do ATS, a empresa confere, a vaga aparece).
2. **Detectada != aceita.** Toda evidencia encontrada e persistida com seus
   sinais, inclusive a fraca. Quem decide e `assert_confirmation_evidence`, no
   caminho de `transition` — o mesmo portao para qualquer comando futuro.
3. **O conteudo observado nao sobrevive a observacao.** Corpo, assunto e
   remetente existem em memoria durante o matching e nunca sao persistidos: o
   que fica e a referencia opaca, o instante e os TOKENS dos sinais.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Protocol, Sequence

from .application import ApplicationDomainError, ApplicationService, assert_confirmation_evidence
from .models import (
    Application,
    ApplicationState,
    ConfirmationSource,
    Job,
    SubmissionConfirmationEvidence,
)
from .persistence import Database
from .providers import ProviderError, profile_for


class ConfirmationObservationError(ValueError):
    """A reconciliacao nao pode rodar. Nenhum estado muda."""


class ConfirmationSourceUnavailable(RuntimeError):
    """A fonte nao respondeu: 401, 5xx, timeout, refresh recusado.

    Isto e DELIBERADAMENTE um erro, e nao um resultado vazio. "Nao consegui
    olhar" e "olhei e nao havia nada" sao coisas diferentes, e so a segunda pode
    ser lida como ausencia de confirmacao. Um erro da API nunca pode virar
    "nao houve submissao".
    """


#: Vocabulario de RECEBIMENTO. Nao e palavra de marketing: e a linguagem que um
#: ATS usa para dizer que a candidatura chegou. Comparada sobre texto
#: normalizado (sem acento, minusculo).
CONFIRMATION_PHRASES = (
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "we received your application",
    "we have received your application",
    "your application has been received",
    "application received",
    "your application was submitted",
    "application has been submitted",
    "obrigado por se candidatar",
    "obrigado por sua candidatura",
    "recebemos sua candidatura",
    "sua candidatura foi recebida",
    "candidatura recebida",
)

#: Linguagem de RECUSA. Uma mensagem que recusa nao confirma, mesmo contendo
#: agradecimento ("thank you for your interest... unfortunately"). Sem esta
#: lista, "obrigado" viraria confirmacao de uma candidatura rejeitada.
REJECTION_PHRASES = (
    "we regret",
    "unfortunately",
    "not moving forward",
    "will not be moving forward",
    "decided not to proceed",
    "other candidates",
    "keep your resume on file",
    "infelizmente",
    "nao seguiremos",
    "seguiremos com outros",
    "banco de talentos",
)

#: Pesos por sinal. A frase e PORTA (sem ela nao ha candidato) e, sozinha,
#: fica ABAIXO do piso de aceitacao do dominio: e o que obriga corroboracao.
#: O tempo e corroboracao fraca de proposito — ele so afasta e-mail antigo, nao
#: identifica ninguem.
CONFIRMATION_SIGNAL_WEIGHTS: dict[str, float] = {
    "application_confirmation_phrase": 0.35,
    "company_match": 0.20,
    "ats_domain_match": 0.20,
    "job_match": 0.20,
    "time_window_match": 0.05,
}

#: Tolerancia ANTERIOR ao instante do relato, para desalinhamento de relogio e
#: granularidade de timestamp do provedor. A janela comeca aqui e termina agora:
#: procurar em todo o historico da caixa faria um e-mail antigo da mesma empresa
#: confirmar uma candidatura nova.
CONFIRMATION_WINDOW_TOLERANCE = timedelta(minutes=15)

_MIN_COMPANY_TOKEN = 3
_MIN_JOB_TOKEN = 4


def _instant(value: str) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"not an ISO-8601 instant: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"instant must carry an explicit timezone offset: {value!r}")
    return parsed


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text).casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _tokens(text: str, *, minimum: int = 3) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", _normalize(text))
        if len(token) >= minimum
    }


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize(text))


@dataclass(frozen=True)
class EmailMessage:
    """Uma mensagem, como o observador a recebeu.

    `body` existe para o matching e NAO e persistido. `reference` e a
    referencia: quando ja e um token opaco (o id da API do Gmail, por exemplo)
    ele e usado como esta; um `Message-ID` RFC, que pode carregar o dominio do
    remetente, vira digest.
    """

    reference: str
    observed_at: str
    sender: str = ""
    subject: str = ""
    body: str = ""
    thread_id: str = ""

    def __post_init__(self) -> None:
        if not self.reference:
            raise ValueError("email message requires a reference")
        _instant(self.observed_at)

    @property
    def sender_domain(self) -> str:
        address = self.sender.rsplit("<", 1)[-1].rstrip(">").strip() if "<" in self.sender else self.sender
        return address.rsplit("@", 1)[-1].casefold().strip() if "@" in address else ""


class EmailSource(Protocol):
    """Porto de leitura da caixa. Gmail e uma implementacao; fixture e outra."""

    #: Rotulo que vai para a evidencia (`gmail`, `imap`, `email`).
    provider: str

    def messages_since(self, since: datetime) -> Iterable[EmailMessage]: ...


class ConfirmationObserver(Protocol):
    """Quem observa o mundo e devolve evidencia. Independente de Gmail."""

    def observe(self, application: Application, *, since: datetime) -> list[SubmissionConfirmationEvidence]: ...


class StaticEmailSource:
    """Caixa em memoria: fixture de teste e backfill de mensagens ja colhidas."""

    provider = "email"

    def __init__(self, records: Sequence[EmailMessage]):
        self.records = tuple(records)

    def messages_since(self, since: datetime) -> list[EmailMessage]:
        return [message for message in self.records if _instant(message.observed_at) >= since]


#: Tokens de sinal que o observador de e-mail pode emitir. Conjunto fechado: um
#: sinal novo exige decisao explicita, porque e ele que explica o score.
EMAIL_SIGNALS = tuple(CONFIRMATION_SIGNAL_WEIGHTS)


def opaque_reference(message: EmailMessage) -> str:
    """Referencia opaca: o id quando ja e opaco, senao um digest dele."""
    raw = str(message.reference or message.thread_id)
    if re.fullmatch(r"[a-z0-9_.:-]{1,64}", raw):
        return raw
    return "msg-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def ats_domains(ats: str) -> set[str]:
    """Dominios do proprio ATS, derivados do perfil declarado.

    Nao ha lista de dominio de ATS aqui: `providers` ja e o modulo que sabe onde
    cada ATS vive. Um board que troque de fornecedor de e-mail nao deve exigir
    edicao neste modulo.
    """
    if not ats:
        return set()
    try:
        profile = profile_for(ats)
    except ProviderError:
        return set()
    if not profile.submit_origin:
        return set()
    host = profile.submit_origin.split("://", 1)[-1].split("/", 1)[0].casefold()
    labels = [label for label in host.split(".") if label]
    domains = {host}
    if len(labels) >= 2:
        domains.add(".".join(labels[-2:]))
    return domains


def sender_is_ats(sender_domain: str, domains: set[str]) -> bool:
    """O remetente pertence ao ATS, incluindo subdominio de ENVIO.

    O endereco real de confirmacao costuma ser um subdominio do provedor
    (`no-reply@hire.lever.co`), e comparar por igualdade perdia exatamente o caso
    real. A comparacao e por rotulo: `.lever.co` casa `hire.lever.co`, e nao casa
    `notlever.co` nem `lever.co.evil.com`.
    """
    if not sender_domain or not domains:
        return False
    return any(sender_domain == domain or sender_domain.endswith("." + domain) for domain in domains)


@dataclass(frozen=True)
class EmailMatch:
    """O casamento de uma mensagem, com os sinais que o sustentam."""

    signals: tuple[str, ...]
    confidence: float

    @property
    def corroborations(self) -> tuple[str, ...]:
        return tuple(signal for signal in self.signals if signal != "application_confirmation_phrase")


def match_email(
    message: EmailMessage,
    *,
    job: Job,
    ats: str,
    since: datetime,
    now: datetime | None = None,
) -> EmailMatch | None:
    """Casa uma mensagem com a candidatura. `None` quando nao ha candidato.

    A frase de recebimento e a PORTA: sem ela nao existe evidencia, porque
    "chegou algo da empresa" nao e confirmacao de nada. Com ela, cada
    corroboracao independente soma — e a soma sozinha ainda pode ficar abaixo do
    piso de aceitacao, que e onde "detectada" vira "aceita".
    """
    reference = now or datetime.now(timezone.utc)
    received = _instant(message.observed_at)
    if not since <= received <= reference:
        # Fora da janela nao e evidencia fraca: nao e evidencia. Um e-mail antigo
        # da mesma empresa confirmaria uma candidatura que ele nao confirma.
        return None
    text = f"{message.subject}\n{message.body}"
    normalized = _normalize(text)
    if any(_normalize(phrase) in normalized for phrase in REJECTION_PHRASES):
        return None
    if not any(_normalize(phrase) in normalized for phrase in CONFIRMATION_PHRASES):
        return None
    signals = ["application_confirmation_phrase"]

    sender_tokens = _tokens(message.sender_domain) | _tokens(message.sender)
    text_tokens = _tokens(text)
    haystack = sender_tokens | text_tokens | {_compact(text), _compact(message.sender)}

    company_keys = {key for key in _tokens(job.company, minimum=_MIN_COMPANY_TOKEN)}
    if job.company:
        company_keys.add(_compact(job.company))
    if company_keys & haystack:
        signals.append("company_match")

    domains = ats_domains(ats)
    if sender_is_ats(message.sender_domain, domains):
        signals.append("ats_domain_match")

    job_keys = _tokens(job.title, minimum=_MIN_JOB_TOKEN)
    if job.external_id:
        job_keys.add(_compact(job.external_id))
    if job_keys & (text_tokens | {_compact(text)}):
        signals.append("job_match")

    signals.append("time_window_match")

    ordered = tuple(signal for signal in EMAIL_SIGNALS if signal in signals)
    confidence = min(round(sum(CONFIRMATION_SIGNAL_WEIGHTS[signal] for signal in ordered), 4), 1.0)
    return EmailMatch(ordered, confidence)


class EmailConfirmationObserver:
    """Observador de confirmacao por e-mail, independente do provedor de caixa."""

    def __init__(self, source: EmailSource, *, job: Job, ats: str = "", provider: str | None = None):
        self.source = source
        self.job = job
        self.ats = ats
        # O rotulo vem da FONTE (`gmail`, `imap`, `email`): quem observou e
        # propriedade de quem leu a caixa, nao do observador generico.
        self.provider = provider or getattr(source, "provider", "email")

    def observe(self, application: Application, *, since: datetime) -> list[SubmissionConfirmationEvidence]:
        now = datetime.now(timezone.utc)
        evidence: list[SubmissionConfirmationEvidence] = []
        for message in self.source.messages_since(since):
            match = match_email(message, job=self.job, ats=self.ats, since=since, now=now)
            if match is None:
                continue
            try:
                evidence.append(
                    SubmissionConfirmationEvidence(
                        source=ConfirmationSource.CONFIRMATION_EMAIL,
                        observed_at=message.observed_at,
                        reference=opaque_reference(message),
                        provider=self.provider,
                        confidence=match.confidence,
                        signals=match.signals,
                    )
                )
            except ValueError:
                # Registro hostil ou quebrado (timestamp no futuro, id absurdo)
                # nao derruba a reconciliacao nem vira evidencia.
                continue
        return evidence


@dataclass(frozen=True)
class ConfirmationReconciliation:
    """O que a reconciliacao observou, o que persistiu e o que decidiu."""

    application_id: str
    state: str
    since: str
    detected: tuple[dict[str, Any], ...] = ()
    accepted: dict[str, Any] | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "state": self.state,
            "since": self.since,
            "detected": [dict(item) for item in self.detected],
            "accepted": dict(self.accepted) if self.accepted else None,
            "detail": self.detail,
        }


class ConfirmationReconciliationService:
    """Roda os observadores, persiste o que foi visto e submete ao portao."""

    def __init__(self, database: Database):
        self.database = database

    def reported_at(self, application_id: str) -> datetime:
        """Instante do MANUAL_SUBMISSION_REPORTED: a janela comeca aqui.

        Vem do journal de estados, e nao de um parametro: a janela precisa ser a
        do relato que existe, nao a que quem chama lembrar de informar.
        """
        events = [
            event
            for event in self.database.list_application_events(application_id)
            if event.event == "manual_submission_reported"
        ]
        if not events:
            raise ConfirmationObservationError(
                "confirmation reconciliation requires a recorded manual submission report"
            )
        return _instant(events[-1].created_at)

    def reconcile(
        self,
        application_id: str,
        *,
        email_sources: Sequence[EmailSource] = (),
        observers: Sequence[ConfirmationObserver] = (),
    ) -> ConfirmationReconciliation:
        application = self.database.get_application(application_id)
        if application is None:
            raise ConfirmationObservationError(f"application not found: {application_id}")
        if application.state is not ApplicationState.AWAITING_SUBMISSION_CONFIRMATION:
            raise ConfirmationObservationError(
                "confirmation reconciliation requires AWAITING_SUBMISSION_CONFIRMATION, "
                f"got {application.state.value}"
            )
        job = self.database.get_job(application.job_id)
        if job is None:
            raise ConfirmationObservationError(f"job not found: {application.job_id}")
        since = self.reported_at(application_id) - CONFIRMATION_WINDOW_TOLERANCE

        active: list[ConfirmationObserver] = list(observers)
        active.extend(
            EmailConfirmationObserver(source, job=job, ats=str(job.source))
            for source in email_sources
        )
        if not active:
            raise ConfirmationObservationError("confirmation reconciliation requires at least one observer")

        delivered: list[SubmissionConfirmationEvidence] = []
        for observer in active:
            try:
                delivered.extend(observer.observe(application, since=since))
            except ConfirmationSourceUnavailable as exc:
                # Falha de fonte NAO e ausencia de evidencia: propaga como erro
                # explicito, e nada e persistido. A Application continua onde
                # estava, e o operador sabe que a pergunta ficou sem resposta.
                raise ConfirmationSourceUnavailable(
                    f"confirmation source unavailable for {application_id}: {exc}"
                ) from exc

        # Detectada != aceita: TUDO o que foi visto e persistido antes de qualquer
        # decisao, inclusive o que nao alcanca o piso. Recusar nao pode significar
        # apagar.
        for item in delivered:
            self.database.save_confirmation_evidence(application_id, item, accepted=False)

        if not delivered:
            return ConfirmationReconciliation(
                application_id, application.state.value, since.isoformat(timespec="seconds"),
                detail="no confirmation evidence observed in the window",
            )

        best = max(delivered, key=lambda item: item.confidence)
        try:
            assert_confirmation_evidence(best)
        except ApplicationDomainError as exc:
            return ConfirmationReconciliation(
                application_id, application.state.value, since.isoformat(timespec="seconds"),
                detected=tuple(item.safe_view() for item in delivered),
                detail=f"observed but not accepted: {exc}",
            )

        ApplicationService(self.database).confirm_submission(application_id, best)
        self.database.save_confirmation_evidence(application_id, best, accepted=True)
        state = self.database.get_application(application_id).state.value
        return ConfirmationReconciliation(
            application_id, state, since.isoformat(timespec="seconds"),
            detected=tuple(item.safe_view() for item in delivered),
            accepted=best.safe_view(),
            detail="accepted as independent confirmation evidence",
        )
