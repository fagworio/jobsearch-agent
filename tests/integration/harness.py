"""Harness dos testes de integração do `ApplicationLoop`.

O loop destes testes é o **real** — `ApplicationLoop` sobre `LoopRuntime` — e o
guard que observa desafio é o `challenge-guard` real, num Chromium real. O que é
substituído é só o destino controlado (adapter, sessão de loopback, política,
endereço de submit) e, quando o teste quer, o `sleep` do gate — que é como o
teste simula a PESSOA resolvendo o desafio na janela:

    o gate dorme entre duas observações; o hook de sleep remove o marcador do
    DOM na MESMA thread do loop (a API síncrona do Playwright é presa à thread,
    então mutar o DOM de outro thread não seria uma simulação válida).

Isso não é dublê do guard nem do gate: é o comportamento real de "o desafio
sumiu da página" — o mesmo que um clique humano produz.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jobsearch_agent.challenge_gate import PreSubmitChallengeGate
from jobsearch_agent.live_view import LiveViewRelay
from jobsearch_agent.loop import ApplicationLoop, LoopRuntime, PreparedMaterial
from jobsearch_agent.models import (
    ApplicationAnswer,
    ApplicationState,
    CandidatePreferences,
    CareerProfile,
    Experience,
    Job,
)
from jobsearch_agent.persistence import Database
from jobsearch_agent.qa import AnswerKnowledgeBase
from jobsearch_agent.resolver import GroundedTemplateProvider
from jobsearch_agent.submission import LiveNetworkPolicy
from tests.e2e.session import LoopbackSession
from tests.integration.challenge_ats import ChallengeCapableATS, JOB_ID, resume_pdf

SUBMITTED = ApplicationState.SUBMITTED.value


def candidate() -> CareerProfile:
    """Perfil sintético e explícito: nada vem do perfil local do candidato."""
    return CareerProfile(
        identity={
            "name": "Integration Candidate",
            "first_name": "Integration",
            "last_name": "Candidate",
            "email": "integration.candidate@example.invalid",
            "phone": "+55 11 90000-0000",
            "current_location": "São Paulo, Brazil",
            "country": "Brazil",
            "linkedin": "https://www.linkedin.com/in/integration-candidate",
        },
        professional_summary={"en-US": "WordPress developer with plugin experience."},
        experiences=[Experience(id="exp-1", company="Acme", role="WordPress Developer", start_date="2019-01")],
        skills={"wordpress": {"years": "7", "level": "advanced", "tags": ["wordpress", "php"]}},
        languages={"english": {"level": "advanced"}},
        demo=False,
    )


def preferences() -> CandidatePreferences:
    return CandidatePreferences(work_authorization=["Brazil"], requires_sponsorship="no")


def answers() -> AnswerKnowledgeBase:
    return AnswerKnowledgeBase(
        [
            ApplicationAnswer(
                question_key="q-resume-note",
                question="Anything else we should know?",
                answer="Available immediately.",
                source="approved_answer",
                confidence=1.0,
                approved=True,
            )
        ]
    )


@dataclass
class Harness:
    """Tudo o que um cenário precisa para afirmar algo sobre o loop real."""

    loop: ApplicationLoop
    ats: ChallengeCapableATS
    database: Database
    sessions: list = field(default_factory=list)
    guards: list = field(default_factory=list)
    resume_path: Path = Path()
    resume_sha256: str = ""
    job_id: str = ""
    gate: PreSubmitChallengeGate | None = None
    relay: LiveViewRelay | None = None
    observed: list = field(default_factory=list)

    def run(self, *, submit: bool = True):
        return self.loop.run(self.job_id, submit=submit)

    def runtime_session(self):
        """Abre a sessão do loop (browser + página) sem rodar o loop.

        Existe para os testes que precisam observar a página real — o
        orquestrador, por exemplo — sem depender de um ciclo completo.
        """
        job = self.database.get_job(self.job_id)
        adapter = __import__("jobsearch_agent.ats", fromlist=["GreenhouseAdapter"]).GreenhouseAdapter()
        session = self.loop.runtime.open_session(job, adapter)
        # Quem navega ate o formulario e o orquestrador do loop; ao abrir a
        # sessao por fora, a navegacao e nossa — senao a pagina fica em branco.
        session.open(self.loop.runtime.form_url(job, adapter))
        return session

    def application(self):
        return self.database.get_application_for_job(self.job_id)

    def state(self) -> ApplicationState:
        return self.application().state

    def writes(self) -> int:
        """Escritas de submissão autorizadas e consumidas, do guard real."""
        total = 0
        for guard in self.guards:
            if guard is not None:
                total += int(getattr(guard, "authorized_writes_used", 0) or 0)
        return total

    def marker_present(self) -> bool:
        if not self.sessions:
            return False
        page = getattr(self.sessions[-1], "page", None)
        if page is None:
            return False
        return bool(page.evaluate("() => !!document.getElementById('gate-challenge')"))


def _seed_job(database: Database, url: str) -> str:
    job = Job(
        id="job-integration-001",
        source="greenhouse",
        external_id=JOB_ID,
        company="Controlled Integration ATS",
        title="WordPress Developer",
        description="Build WordPress sites and plugins.",
        url=url,
    )
    database.save_job(job, f"greenhouse:{JOB_ID}", {})
    return job.id


def build_harness(
    tmp_path: Path,
    ats: ChallengeCapableATS,
    *,
    resolution_enabled: bool = True,
    captcha_wait: float = 0.0,
    poll_seconds: float = 0.05,
    on_wait: Callable[[Any], None] | None = None,
) -> Harness:
    """Monta o loop real contra o ATS controlado.

    `on_wait(page)` é chamado a cada espera do gate, na thread do loop: é assim
    que o cenário "a pessoa resolveu" remove o marcador do DOM.
    """
    database = Database(tmp_path / "integration.db")
    resume_path = tmp_path / "resume.pdf"
    resume_sha256 = hashlib.sha256(resume_pdf(resume_path)).hexdigest()
    sessions: list = []
    guards: list = []

    def open_session(job, adapter):
        session = LoopbackSession(ats.origin)
        session.start()
        sessions.append(session)
        guards.append(session.network_guard)
        return session

    def policy_for(provider, application_id, intent_id):
        return LiveNetworkPolicy(
            provider=provider,
            allowed_origin=ats.origin,
            allowed_path_pattern=rf"^/jobs/{JOB_ID}/apply$",
            allowed_method="POST",
            allowed_stage="SUBMIT",
            application_id=application_id,
            submission_intent_id=intent_id,
        )

    gate: PreSubmitChallengeGate | None = None
    relay: LiveViewRelay | None = None
    observed: list = []
    if resolution_enabled:
        relay = LiveViewRelay()

        def sleep(seconds: float) -> None:
            page = getattr(sessions[-1], "page", None) if sessions else None
            if on_wait is not None and page is not None:
                on_wait(page, observed)
            if seconds > 0:
                time.sleep(seconds)

        gate = PreSubmitChallengeGate(poll_seconds=poll_seconds, sleep=sleep, relay=relay)

    runtime = LoopRuntime(
        adapter_for=lambda job: __import__("jobsearch_agent.ats", fromlist=["GreenhouseAdapter"]).GreenhouseAdapter(),
        form_url=lambda job, adapter: ats.apply_url,
        submission_destination=lambda job, adapter, form: ats.apply_url,
        profile=candidate(),
        preferences=preferences(),
        answers=answers(),
        prepare=lambda job: PreparedMaterial(
            resume_path=str(resume_path),
            resume_sha256=resume_sha256,
            artifact_root=str(tmp_path),
            validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        ),
        open_session=open_session,
        policy_for=policy_for,
        answer_provider=GroundedTemplateProvider(),
        allow_insecure_destination=True,
        allow_advance=False,
        challenge_gate=gate,
        challenge_wait_seconds=captcha_wait,
        live_view_relay=relay,
        # Curto de proposito: quando nada sai, o submitter observa ate o
        # deadline. Num teste isso tem de ser segundos, nao os 45 do padrao.
        submission_timeout=1.5,
    )
    job_id = _seed_job(database, ats.apply_url)
    return Harness(
        loop=ApplicationLoop(database, runtime),
        ats=ats,
        database=database,
        sessions=sessions,
        guards=guards,
        resume_path=resume_path,
        resume_sha256=resume_sha256,
        job_id=job_id,
        gate=gate,
        relay=relay,
        observed=observed,
    )


def remove_challenge_marker(page: Any, observed: list | None = None) -> None:
    """O que um clique humano produz: o desafio deixa de estar na página.

    `observed` (opcional) recebe fatos do DOM lidos NA THREAD DA PÁGINA — é a
    única forma legítima de um teste inspecionar a página enquanto o loop roda.
    """
    page.evaluate("() => { const el = document.getElementById('gate-challenge'); if (el) el.remove(); }")
    if observed is not None:
        observed.append({"locked": page.evaluate("() => document.querySelectorAll('[data-liveview-locked]').length")})


__all__ = [
    "Harness",
    "SUBMITTED",
    "answers",
    "build_harness",
    "candidate",
    "preferences",
    "remove_challenge_marker",
]
