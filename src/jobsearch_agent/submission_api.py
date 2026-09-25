"""Canal de API do ATS — com guard PROPRIO, e nao o guard do browser.

O `NetworkWriteGuard` protege uma pagina viva: intercepta requisicoes, olha DOM,
frames e respostas, e existe para provar que UMA escrita de UI saiu daquele
browser. Aqui nao ha pagina, DOM, clique, formulario nem desafio. O que existe:

    orcamento de UMA requisicao HTTP, ligado a uma intent autorizada
    transporte que NAO segue redirect e NAO repete tentativa
    contabilidade de intent e tentativa pela mesma porta do dominio
    (`SubmissionService`), para nao existir uma segunda regra de exactly-once

Reutilizar o guard do browser aqui acoplaria a contabilidade de intencao a uma
fronteira que este canal nao tem — e faria uma escrita HTTP depender de um
`page` que nunca existiu. O paralelo e deliberado: mesma disciplina, pecas
proprias.

O credencial e o ponto honesto deste modulo: o endpoint de candidatura dos ATS
EXIGE credencial da empresa/parceiro (medido: `POST` sem auth ao board do
Greenhouse -> `401 HTTP Basic: Access denied`). Sem credencial declarada NAO ha
canal de API — `DomainPolicy.channel_for` degrada para `BROWSER`. E o segredo
nunca entra em evento, evidencia, log ou `repr`: o que a auditoria guarda e que
havia credencial, de que tipo, para qual dominio.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

from .models import Application, ApplicationForm, ValidationResult

from .persistence import Database
from .submission import (
    SubmissionBoundaryError,
    SubmissionService,
    SubmissionVerification,
    build_review_snapshot,
    build_submission_payload,
    review_field_rows,
)
from .coordinator import SubmissionResult
from .submission_policy import DEFAULT_POLICY, Channel, DomainPolicy

#: Motivos de falha que este canal pode emitir. O conjunto FECHADO que a
#: auditoria aceita e declarado no DOMINIO (`submission.API_REASON_TOKENS`), e
#: nao aqui: uma ponte entre os dois e verificada em
#: `tests/test_submission_api.py::test_every_reason_token_this_channel_can_emit_is_accepted_by_the_domain`,
#: para que um token novo nao nasca recusado em tempo de execucao.


class FormSource(Protocol):
    """Fonte do contrato acumulado de respostas (o `ApplicationJourney`)."""

    def as_form(self) -> ApplicationForm: ...


class JobContext(Protocol):
    """O minimo que o snapshot audita sobre a vaga."""

    company: str
    title: str


class ApiSubmissionError(RuntimeError):
    """Recusa do canal de API ANTES de qualquer escrita.

    Nao e um desfecho de submissao: e a tentativa de escrever sem autorizacao
    (sem credencial, destino divergente, orcamento estourado). Levanta em vez de
    virar `failed` para nao registrar no banco uma tentativa que nunca existiu.
    """


# -- credencial -----------------------------------------------------------------


@dataclass(frozen=True)
class ApiCredential:
    """Credencial de API de UM dominio. O segredo nao sai daqui.

    `scheme` e `basic` (usuario + segredo) ou `bearer`. Nao ha campo livre de
    header: um header configuravel transportaria o segredo para lugares que
    ninguem audita, e o erro de digitacao viraria vazamento.
    """

    domain: str
    secret: str
    scheme: str = "bearer"
    username: str = ""

    def __post_init__(self) -> None:
        if not self.domain.strip():
            raise ApiSubmissionError("API credential requires a domain")
        if not self.secret:
            raise ApiSubmissionError("API credential requires a secret")
        if self.scheme not in {"basic", "bearer"}:
            raise ApiSubmissionError(f"unsupported API credential scheme: {self.scheme}")
        if self.scheme == "basic" and not self.username:
            raise ApiSubmissionError("basic API credential requires a username")

    def __repr__(self) -> str:
        return f"ApiCredential(domain={self.domain!r}, scheme={self.scheme!r}, secret=<redacted>)"

    __str__ = __repr__

    def matches(self, destination: str) -> bool:
        """A credencial vale para este destino? Host exato ou subdominio."""
        host = (urlsplit(destination).hostname or "").strip().casefold()
        domain = self.domain.strip().casefold()
        return bool(host) and bool(domain) and (host == domain or host.endswith("." + domain))

    def header(self) -> tuple[str, str]:
        """Par header/valor. Chamado apenas no momento do envio."""
        if self.scheme == "basic":
            raw = f"{self.username}:{self.secret}".encode()
            return "Authorization", "Basic " + base64.b64encode(raw).decode("ascii")
        return "Authorization", f"Bearer {self.secret}"

    def redacted(self) -> dict[str, str]:
        """O que pode ser auditado: tipo e dominio, nunca o valor."""
        return {"credential_scheme": self.scheme, "credential_domain": self.domain}


# -- politica e guard proprios --------------------------------------------------


@dataclass(frozen=True)
class ApiWritePolicy:
    """O que esta autorizado a sair: um metodo, um destino exato, uma intent.

    Nao conhece browser, pagina ou desafio. `origin` e `path` sao exatos (nao ha
    padrao curinga): uma escrita de API e um endpoint, nao uma familia de URLs.
    """

    provider: str
    application_id: str
    submission_intent_id: str
    destination: str
    method: str = "POST"
    allowed_stage: str = "SUBMIT"
    allow_insecure_destination: bool = False

    @property
    def parsed(self) -> SplitResult:
        return urlsplit(self.destination)

    @property
    def origin(self) -> str:
        parsed = self.parsed
        return f"{parsed.scheme}://{parsed.netloc}"

    def validate(self, method: str, url: str, stage: str) -> ValidationResult:
        errors: list[str] = []
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if method.upper() != self.method.upper():
            errors.append("unexpected API submission method")
        if origin != self.origin:
            errors.append("unexpected API submission origin")
        if parsed.path != self.parsed.path:
            errors.append("unexpected API submission path")
        if stage != self.allowed_stage:
            errors.append("unexpected API submission stage")
        if parsed.username or parsed.password:
            errors.append("API submission URL must not contain credentials")
        if parsed.scheme != "https" and not (self.allow_insecure_destination and loopback):
            errors.append("API submission destination must be HTTPS")
        if parsed.query and parsed.query != self.parsed.query:
            errors.append("unexpected API submission query")
        return ValidationResult(not errors, "OK" if not errors else "BLOCKED_UNEXPECTED_WRITE", errors)


@dataclass
class ApiWriteGuard:
    """Orcamento de UMA escrita, consumido ANTES do envio.

    Consumir antes e o ponto: se o transporte estourar no meio, a requisicao pode
    ter chegado ao provedor. O orcamento tem de refletir a TENTATIVA, nao o
    sucesso — senao um `except` viraria permissao para uma segunda escrita.
    """

    policy: ApiWritePolicy
    max_writes: int = 1
    writes_used: int = 0

    @property
    def exhausted(self) -> bool:
        return self.writes_used >= self.max_writes

    def authorize(self, method: str, url: str, stage: str = "SUBMIT") -> None:
        if self.exhausted:
            raise ApiSubmissionError("API write budget exhausted: a second write is not authorized")
        result = self.policy.validate(method, url, stage)
        if not result.valid:
            raise ApiSubmissionError("; ".join(result.errors))
        if url != self.policy.destination:
            raise ApiSubmissionError("API destination does not match the authorized destination")
        self.writes_used += 1


# -- transporte -----------------------------------------------------------------


@dataclass(frozen=True)
class ApiRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    fields: Mapping[str, str | list[str]] = field(default_factory=dict)
    files: Mapping[str, tuple[str, bytes, str]] = field(default_factory=dict)
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class ApiResponse:
    status_code: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


class ApiTransport(Protocol):
    """Um envio, sem politica propria: quem decide ja decidiu antes de chamar."""

    def send(self, request: ApiRequest) -> ApiResponse: ...


class HttpxApiTransport:
    """HTTP real. `follow_redirects=False` e a regra, nao um detalhe de config.

    Um redirect depois do POST seria uma SEGUNDA escrita, para outra origem, que
    nenhuma intent autorizou. Seguir o redirect transformaria o guard em
    decoracao: o orcamento teria sido consumido por uma escrita e o efeito teria
    vindo de outra.
    """

    def send(self, request: ApiRequest) -> ApiResponse:
        import httpx

        response = httpx.request(
            request.method,
            request.url,
            headers=dict(request.headers),
            data={key: value for key, value in request.fields.items()},
            files=dict(request.files),
            timeout=request.timeout_seconds,
            follow_redirects=False,
        )
        return ApiResponse(response.status_code, response.content, dict(response.headers))


@dataclass(frozen=True)
class ApiSubmissionOutcome:
    """Desfecho do canal de API, com a BASE da confirmacao declarada."""

    status: str
    http_status: int | None = None
    writes: int = 0
    evidence: dict[str, object] = field(default_factory=dict)
    error: str = ""
    reason_token: str = ""

    def as_journal(self) -> dict[str, object]:
        return {
            "status": self.status,
            "http_status": self.http_status,
            "writes": self.writes,
            "reason_token": self.reason_token,
            "evidence": dict(self.evidence),
        }


# -- transporte autorizado ------------------------------------------------------


class ApiSubmitter:
    """Executa UMA requisicao autorizada e classifica a resposta.

    A classificacao e conservadora por desenho:

    ```text
    2xx        confirmed   o provedor respondeu a escrita autorizada
    3xx        unknown     redirect NAO seguido; a escrita saiu e nao se sabe o efeito

    A divergencia do canal de browser no 3xx e deliberada: la um redirect que
    chega a pagina de confirmacao e o caminho normal de sucesso (`confirmation_
    reached`); aqui nao ha pagina para observar, e seguir o `Location` seria uma
    segunda escrita que nenhuma intent autorizou.
    401/403    failed      auth/recusa explicita (token proprio)
    429        failed      recusado por limite; nada foi aceito
    outro 4xx  failed      recusa explicita do pedido
    5xx        unknown     o provedor pode ter processado antes de falhar
    excecao    unknown     a requisicao pode ter saido; NAO se repete

    Em `unknown` a evidencia grava o par `submit_write=True` /
    `confirmed_submission=False`: escrito, nao confirmado. Um `False` sozinho
    seria lido como "nao foi enviada", que e exatamente o que nao se sabe.
    ```

    `unknown` nunca vira `confirmed`, e nenhum caminho reenvia. A base da
    confirmacao vai na evidencia (`confirmation_basis`), porque "2xx" e a base
    MEDIDA deste canal — o vocabulario de aceitacao por provider segue NAO
    medido, e chamar isso de confirmacao plena seria afirmar mais do que se sabe.
    """

    def __init__(self, transport: ApiTransport, *, timeout_seconds: float = 30.0):
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def submit(self, *, guard: ApiWriteGuard, request: ApiRequest) -> ApiSubmissionOutcome:
        guard.authorize(request.method, request.url)
        prepare = ApiRequest(
            request.method,
            request.url,
            dict(request.headers),
            dict(request.fields),
            dict(request.files),
            request.timeout_seconds or self.timeout_seconds,
        )
        try:
            response = self.transport.send(prepare)
        except Exception as exc:  # transporte e fronteira: qualquer falha e desfecho
            return ApiSubmissionOutcome(
                status="unknown",
                http_status=None,
                writes=guard.writes_used,
                evidence={
                    "reason": f"transport error: {type(exc).__name__}",
                    "write_may_have_been_sent": True,
                    # O par explicito: a escrita TENTOU sair e nao ha confirmacao.
                    # `confirmed_submission=False` aqui nao significa "nao foi
                    # enviada" — significa "ninguem confirmou", que e o fato.
                    "submit_write": True,
                    "confirmed_submission": False,
                },
                error=f"{type(exc).__name__}",
                reason_token="api_transport_error",
            )
        return self._classify(response, writes=guard.writes_used)

    def _classify(self, response: ApiResponse, *, writes: int) -> ApiSubmissionOutcome:
        status = int(response.status_code)
        if 200 <= status <= 299:
            evidence: dict[str, object] = {
                "status_code": status,
                "confirmation_basis": "http_status",
                "confirmation_markers": "unmeasured",
                "body_bytes": len(response.body),
            }
            keys = _json_keys(response.body)
            if keys:
                evidence["body_keys"] = keys
            return ApiSubmissionOutcome(status="confirmed", http_status=status, writes=writes, evidence=evidence)
        if 300 <= status <= 399:
            location = _origin_only(response.headers.get("location", ""))
            evidence = {"redirect_refused": True, "submit_write": True, "confirmed_submission": False}
            if location:
                evidence["redirect_origin"] = location
            return ApiSubmissionOutcome(
                status="unknown",
                http_status=status,
                writes=writes,
                evidence=evidence,
                error="provider answered with a redirect; it was not followed",
                reason_token="api_redirect_not_followed",
            )
        if status == 429:
            return self._failure(status, writes, "api_rate_limited", "provider rate limit")
        if status in {401, 403}:
            return self._failure(status, writes, "api_auth_rejected", "provider rejected the credential")
        if 400 <= status <= 499:
            return self._failure(status, writes, "api_provider_rejected", "provider rejected the submission")
        return ApiSubmissionOutcome(
            status="unknown",
            http_status=status,
            writes=writes,
            evidence={"status_code": status, "submit_write": True, "confirmed_submission": False},
            error="provider server error; the write may have been processed",
            reason_token="api_server_error",
        )

    @staticmethod
    def _failure(status: int, writes: int, token: str, error: str) -> ApiSubmissionOutcome:
        return ApiSubmissionOutcome(
            status="failed",
            http_status=status,
            writes=writes,
            evidence={"status_code": status, "confirmed_submission": False, "submit_write": True},
            error=error,
            reason_token=token,
        )


def _json_keys(body: bytes) -> list[str]:
    """Chaves de topo do JSON, nunca valores: valor de resposta e dado do provedor."""
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return []
    if not isinstance(parsed, dict):
        return []
    return sorted(str(key) for key in parsed)[:20]


def _origin_only(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""


# -- router ---------------------------------------------------------------------


#: Vocabulario do RESULTADO da execucao, compartilhado com o canal de browser:
#: quem le `ApplicationLoopResult.status` nao pode receber dois idiomas
#: diferentes conforme o canal escolhido. O `ApiSubmissionOutcome` mantem o
#: vocabulario da verificacao (`confirmed`/`failed`/`unknown`), que e o que a
#: porta do dominio consome; a traducao acontece uma vez, aqui.
_RESULT_STATUS = {"confirmed": "SUBMITTED", "failed": "SUBMIT_FAILED", "unknown": "SUBMIT_UNKNOWN"}


class SubmissionRouter:
    """Escolhe o canal pelo `DomainPolicy` e executa a escrita autorizada.

    O roteamento e dado, nao heuristica: a API exige credencial DECLARADA para o
    dominio do destino; sem ela a politica degrada para `BROWSER` e este router
    nao escreve nada. LinkedIn e Indeed continuam `HANDOFF` mesmo com credencial.
    """

    def __init__(
        self,
        database: Database,
        *,
        transport: ApiTransport | None = None,
        credentials: Mapping[str, ApiCredential] | None = None,
        timeout_seconds: float = 30.0,
        policy: DomainPolicy = DEFAULT_POLICY,
    ):
        self.database = database
        self.transport = transport
        self.credentials: dict[str, ApiCredential] = dict(credentials or {})
        self.timeout_seconds = timeout_seconds
        self.policy = policy

    # -- decisao de canal ------------------------------------------------------

    def channel_for(self, destination: str) -> Channel:
        host = urlsplit(destination).hostname or ""
        return self.policy.channel_for(host, authorized_domains=tuple(self.credentials))

    def credential_for(self, destination: str) -> ApiCredential | None:
        for credential in self.credentials.values():
            if credential.matches(destination):
                return credential
        return None

    # -- execucao --------------------------------------------------------------

    def submit(
        self,
        *,
        application: Application,
        form: ApplicationForm,
        provider: str,
        destination: str,
        resume_path: str,
        resume_sha256: str,
        resume_field: str,
        form_fingerprint: str,
        answers_fingerprint: str,
        journey: FormSource | None = None,
        job: JobContext | None = None,
        ttl_seconds: int = 300,
        allow_insecure_destination: bool = False,
        credential: ApiCredential | None = None,
    ) -> SubmissionResult:
        """Uma submissao autorizada pelo canal de API.

        A ordem e a mesma do browser — snapshot, intent, autorizacao, tentativa
        ANTES da escrita, desfecho — porque o dominio tem uma unica porta para
        `SUBMITTED` e este canal nao abre outra.
        """
        if self.transport is None:
            raise ApiSubmissionError("API channel has no transport configured")
        if not all((resume_sha256, form_fingerprint, answers_fingerprint)):
            raise SubmissionBoundaryError("submission requires form fingerprint, answers fingerprint and resume SHA256")
        if not destination:
            raise SubmissionBoundaryError("API channel requires a submission destination")
        if not resume_field:
            # O nome do campo de curriculo e POR PROVIDER e nao foi medido em
            # nenhum. Inventar um nome seria escrever um contrato que ninguem
            # verificou, entao ele e exigido de quem chama.
            raise ApiSubmissionError("API channel requires the provider's resume field name")
        resolved_credential = credential or self.credential_for(destination)
        if resolved_credential is None:
            raise ApiSubmissionError(f"no declared API credential for {urlsplit(destination).hostname or destination}")
        if not resolved_credential.matches(destination):
            raise ApiSubmissionError("API credential does not cover the submission destination")
        if self.channel_for(destination) is not Channel.API:
            raise ApiSubmissionError("destination is not authorized for the API channel")

        service = SubmissionService(self.database)
        source = journey.as_form() if journey is not None else form
        resolved_fields, manual_questions = review_field_rows(source)
        service.save_review_snapshot(
            build_review_snapshot(
                application_id=application.id,
                job_id=application.job_id,
                company=str(getattr(job, "company", "") or ""),
                title=str(getattr(job, "title", "") or ""),
                provider=provider,
                destination=destination,
                resume_filename=Path(resume_path).name or "resume.pdf",
                resume_sha256=resume_sha256,
                form_fingerprint=form_fingerprint,
                answers_fingerprint=answers_fingerprint,
                resolved_fields=resolved_fields,
                manual_questions=manual_questions,
            )
        )
        intent = service.create_intent(
            application_id=application.id,
            job_id=application.job_id,
            provider=provider,
            destination=destination,
            form_fingerprint=form_fingerprint,
            resume_sha256=resume_sha256,
            answers_fingerprint=answers_fingerprint,
            expires_in_seconds=ttl_seconds,
            allow_insecure_destination=allow_insecure_destination,
        )
        service.authorize_submission(intent.id)
        write_policy = ApiWritePolicy(
            provider=provider,
            application_id=application.id,
            submission_intent_id=intent.id,
            destination=destination,
            allow_insecure_destination=allow_insecure_destination,
        )
        guard = ApiWriteGuard(write_policy)
        attempt = service.begin_submission(
            intent.id,
            current_form_fingerprint=form_fingerprint,
            current_resume_sha256=resume_sha256,
            current_answers_fingerprint=answers_fingerprint,
            policy=write_policy,
            method=write_policy.method,
            url=destination,
        )
        payload = build_submission_payload(source, extra_files={resume_field: resume_path})
        header, value = resolved_credential.header()
        request = ApiRequest(
            method=write_policy.method,
            url=destination,
            headers={header: value, "Accept": "application/json"},
            fields=payload.fields,
            files=payload.files,
            timeout_seconds=self.timeout_seconds,
        )
        outcome = ApiSubmitter(self.transport, timeout_seconds=self.timeout_seconds).submit(guard=guard, request=request)
        service.record_result(attempt.id, self._verification(outcome))
        return SubmissionResult(
            status=_RESULT_STATUS[outcome.status],
            intent_id=intent.id,
            attempt_id=attempt.id,
            http_status=outcome.http_status,
            writes=outcome.writes,
            evidence=dict(outcome.evidence),
            error=outcome.error,
            transport="api",
        )

    @staticmethod
    def _verification(outcome: ApiSubmissionOutcome) -> SubmissionVerification:
        if outcome.status == "confirmed":
            return SubmissionVerification.confirmed("provider_http_response", dict(outcome.evidence))
        if outcome.status == "failed":
            return SubmissionVerification.failed(outcome.error, reason_token=outcome.reason_token)
        return SubmissionVerification.unknown(outcome.error, reason_token=outcome.reason_token)


__all__ = [
    "ApiCredential",
    "ApiRequest",
    "ApiResponse",
    "ApiSubmissionError",
    "ApiSubmissionOutcome",
    "ApiSubmitter",
    "ApiTransport",
    "ApiWriteGuard",
    "ApiWritePolicy",
    "FormSource",
    "JobContext",
    "HttpxApiTransport",
    "SubmissionRouter",
]
