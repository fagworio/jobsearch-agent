"""Coordenacao da submissao (JSA-LOOP-001).

Antes, a sequencia final vivia dentro de `pipeline._submit_live_in_browser` e o
teste E2E a remontava a mao: snapshot, intent, autorizacao, politica de rede,
clique e observacao. Duas copias do mesmo contrato e uma terceira a caminho (o
`ApplicationLoop`) sao exatamente o que produz divergencia.

Aqui existe uma unica implementacao. Ela continua sendo a Submission Boundary:
valida a intent, persiste a tentativa ANTES da escrita, arma o guard para um
unico POST e observa o desfecho. Nenhum token e forjado nem contornado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .models import Application, ApplicationForm, Job
from .persistence import Database
from .submission import (
    LiveNetworkPolicy,
    SubmissionBoundaryError,
    SubmissionService,
    build_review_snapshot,
    review_field_rows,
)
from .submission_browser import BrowserSubmitter

#: Constroi a politica de escrita para (provider, application_id, intent_id). O
#: padrao e a politica declarada pelo provider; o loop pode injetar outra — e ela
#: continua sendo validada contra a intent em `begin_submission`.
PolicyFactory = Callable[[str, str, str], LiveNetworkPolicy]


@dataclass(frozen=True)
class SubmissionResult:
    """Desfecho de uma submissao, sem detalhe de provider nem de browser."""

    status: str
    intent_id: str = ""
    attempt_id: str = ""
    http_status: int | None = None
    writes: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "status": self.status,
            "http_status": self.http_status,
            "transport": "browser",
            "evidence": dict(self.evidence),
            "error": self.error,
        }


class SubmissionCoordinator:
    """Snapshot → intent → autorizacao → POST unico → observacao → tentativa."""

    def __init__(self, database: Database, *, timeout_seconds: float = 45.0):
        self.database = database
        self.timeout_seconds = timeout_seconds

    def submit(
        self,
        *,
        application: Application,
        job: Job,
        form: ApplicationForm,
        session: Any,
        provider: str,
        destination: str,
        resume_sha256: str,
        form_fingerprint: str,
        answers_fingerprint: str,
        ttl_seconds: int = 300,
        allow_insecure_destination: bool = False,
        policy_factory: "PolicyFactory | None" = None,
    ) -> SubmissionResult:
        """Executa UMA submissao autorizada. Nao decide se deve submeter."""
        if not all((resume_sha256, form_fingerprint, answers_fingerprint)):
            raise SubmissionBoundaryError(
                "submission requires form fingerprint, answers fingerprint and resume SHA256"
            )
        resolved_fields, manual_questions = review_field_rows(form)
        service = SubmissionService(self.database)
        service.save_review_snapshot(
            build_review_snapshot(
                application_id=application.id,
                job_id=application.job_id,
                company=str(job.company or ""),
                title=str(job.title or ""),
                provider=provider,
                destination=destination,
                resume_filename="resume.pdf",
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
        build_policy = policy_factory or LiveNetworkPolicy.for_submission
        network_policy = build_policy(provider, application.id, intent.id)
        outcome = BrowserSubmitter(self.database, timeout_seconds=self.timeout_seconds).submit(
            session,
            intent.id,
            current_form_fingerprint=form_fingerprint,
            current_resume_sha256=resume_sha256,
            current_answers_fingerprint=answers_fingerprint,
            policy=network_policy,
        )
        # A contagem autoritativa vem da propria observacao (o executor le o
        # orcamento da posicao 0 ANTES de desarmar). O fallback soma o uso
        # registrado no guard, que sobrevive ao desarme.
        observed_writes = outcome.evidence.get("authorized_writes_used")
        writes = observed_writes if isinstance(observed_writes, int) else _submission_writes(session)
        return SubmissionResult(
            status=outcome.status,
            intent_id=intent.id,
            attempt_id=outcome.attempt_id,
            http_status=outcome.http_status,
            writes=int(writes),
            evidence=outcome.evidence,
            error=outcome.error,
        )


def _submission_writes(session: Any) -> int:
    """Escritas usadas, do guard — nunca contadas pelo proprio submetedor.

    `authorized_write_usage` nao serve aqui: depois de desarmar, o guard mantem o
    USO mas esvazia a lista de permissoes, e o zip devolveria zero. O total
    sobrevive ao desarme, que e o que permite auditar depois do fato.
    """
    guard = getattr(session, "network_guard", None)
    try:
        return int(getattr(guard, "authorized_writes_used", 0) or 0)
    except (TypeError, ValueError):  # pragma: no cover - defensivo
        return 0
