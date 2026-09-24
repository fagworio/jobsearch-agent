"""Pacote de handoff humano (JSA-CG-016).

O ``challenge-guard`` responde "houve um desafio, e o que a pessoa precisa
fazer". Ele nao sabe — e nao deve saber — nada sobre a candidatura. Este modulo e
o lado do dominio: monta o pacote IMUTAVEL que o humano precisa para terminar o
envio, com o curriculo e as respostas que estavam APROVADAS quando a automacao
parou.

Duas propriedades sustentam o desenho, e as duas sao testadas:

1. **Atomicidade.** O pacote e montado, validado e persistido ANTES de a
   Application sair de ``NEEDS_HUMAN_CAPTCHA``. Se qualquer passo falhar, o
   estado NAO muda: nao existe handoff sem pacote, e nao se cria um estado
   orfao.
2. **Imutabilidade.** O pacote e um bundle proprio (``package.json`` + copia do
   curriculo) endereçado pelo CONTEUDO. Curriculo ou respostas alterados depois
   nao reescrevem o pacote antigo — produzem outro, e o anterior continua
   auditavel.

O pacote completo vive no diretorio privado de artifacts. O journal de eventos
guarda apenas referencia e tokens curtos, nunca respostas: ver
:meth:`HumanHandoffPackage.safe_view`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from urllib.parse import urlsplit

from .application import ApplicationService, context_from_dict
from .challenges import recorded_handoff
from .models import Application, ApplicationForm, ApplicationState, Job, now_iso
from .persistence import Database
from .providers import apply_url as provider_apply_url
from .serialization import canonical_json
from .submission import compute_answers_fingerprint


class HandoffError(ValueError):
    """O handoff nao pode ser montado. A Application permanece onde estava."""


_SAFE_TOKEN = re.compile(r"[a-z0-9_.:-]{1,64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CONTINUATIONS = ("manual", "manual_final")
_ANSWER_KEYS = ("question_key", "question", "answer")
_TEXT_LIMIT = 200
_QUESTION_LIMIT = 500
_ANSWER_LIMIT = 5000
_INSTRUCTION_LIMIT = 240

#: Campos cobertos pelo digest. `package_id` fica de fora: ele ENDERECA o
#: pacote, e o endereco e derivado de um subconjunto deste conteudo.
_CONTENT_FIELDS = (
    "application_id",
    "job_id",
    "provider",
    "reason_token",
    "challenge_session_id",
    "destination",
    "page_url",
    "company",
    "title",
    "resume_path",
    "resume_sha256",
    "answers_fingerprint",
    "approved_answers",
    "continuation",
    "instructions",
    "package_path",
    "created_at",
)


def public_url(value: str, field_name: str) -> str:
    """URL que o humano pode abrir: http(s), sem credencial, query ou fragmento.

    A query e recusada de proposito. Ela nao e necessaria para continuar, e e
    exatamente onde um identificador de sessao ou de provedor viajaria para
    dentro de um artefato de auditoria. O valor NAO e reescrito em silencio: uma
    URL que so funciona com query falha aqui, com o motivo, em vez de virar um
    link quebrado no pacote.
    """
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise HandoffError(f"{field_name} must be an http(s) URL")
    if parts.username or parts.password:
        raise HandoffError(f"{field_name} must not carry credentials")
    if parts.query or parts.fragment:
        raise HandoffError(f"{field_name} must not carry a query or fragment")
    return value


def _digest(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(content)).encode("utf-8")).hexdigest()


def _content_of(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Unico lugar que sabe converter os campos para a forma serializada."""
    data: dict[str, Any] = {}
    for name in _CONTENT_FIELDS:
        value = fields[name]
        if name == "approved_answers":
            value = [row.to_dict() if isinstance(row, HandoffAnswer) else dict(row) for row in value]
        elif name == "instructions":
            value = [str(item) for item in value]
        data[name] = value
    return data


@dataclass(frozen=True)
class HandoffAnswer:
    """Uma resposta aprovada, como o humano vai reler no formulario."""

    question_key: str
    question: str
    answer: str

    def to_dict(self) -> dict[str, str]:
        return {"question_key": self.question_key, "question": self.question, "answer": self.answer}


@dataclass(frozen=True)
class HumanHandoffPackage:
    """Tudo o que o humano precisa para terminar, endereçado pelo conteudo."""

    package_id: str
    application_id: str
    job_id: str
    provider: str
    reason_token: str
    challenge_session_id: str
    destination: str
    page_url: str
    company: str
    title: str
    resume_path: str
    resume_sha256: str
    answers_fingerprint: str
    approved_answers: tuple[HandoffAnswer, ...]
    continuation: str
    instructions: tuple[str, ...]
    package_path: str
    package_sha256: str
    created_at: str

    def __post_init__(self) -> None:
        for name in ("package_id", "provider", "reason_token"):
            if not _SAFE_TOKEN.fullmatch(str(getattr(self, name) or "")):
                raise HandoffError(f"{name} must be a short lowercase token")
        if self.challenge_session_id and not _SAFE_TOKEN.fullmatch(self.challenge_session_id):
            raise HandoffError("challenge_session_id must be a short lowercase token")
        if self.continuation not in _CONTINUATIONS:
            raise HandoffError(f"unsupported handoff continuation: {self.continuation}")
        public_url(self.destination, "destination")
        public_url(self.page_url, "page_url")
        for name in ("resume_sha256", "answers_fingerprint", "package_sha256"):
            if not _SHA256.fullmatch(str(getattr(self, name) or "")):
                raise HandoffError(f"{name} must be a sha256 hex digest")
        if not self.application_id or not self.job_id:
            raise HandoffError("handoff package requires application and job ids")
        if len(self.company) > _TEXT_LIMIT or len(self.title) > _TEXT_LIMIT:
            raise HandoffError("handoff package company/title must be short display text")
        if not self.package_path or not self.resume_path:
            raise HandoffError("handoff package requires its bundle paths")
        for instruction in self.instructions:
            if not str(instruction).strip() or len(str(instruction)) > _INSTRUCTION_LIMIT:
                raise HandoffError("handoff instructions must be short non-empty sentences")
        for row in self.approved_answers:
            if not row.question.strip() or not row.answer.strip():
                raise HandoffError("handoff answers cannot be blank")
            if len(row.question) > _QUESTION_LIMIT or len(row.answer) > _ANSWER_LIMIT:
                raise HandoffError("handoff answer is too long for a review artifact")
        if _digest(self.content()) != self.package_sha256:
            raise HandoffError("handoff package content does not match its digest")

    @classmethod
    def create(cls, **fields: Any) -> "HumanHandoffPackage":
        """A unica porta de construcao: o digest e calculado, nunca aceito."""
        fields = {name: fields[name] for name in (*_CONTENT_FIELDS, "package_id")}
        return cls(**fields, package_sha256=_digest(_content_of(fields)))

    def content(self) -> dict[str, Any]:
        return _content_of({name: getattr(self, name) for name in _CONTENT_FIELDS})

    def to_dict(self) -> dict[str, Any]:
        data = self.content()
        data["package_id"] = self.package_id
        data["package_sha256"] = self.package_sha256
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HumanHandoffPackage":
        rows = tuple(
            HandoffAnswer(*(str(item.get(key, "")) for key in _ANSWER_KEYS))
            for item in data.get("approved_answers", [])
        )
        return cls(
            package_id=str(data["package_id"]),
            approved_answers=rows,
            instructions=tuple(str(item) for item in data.get("instructions", [])),
            package_sha256=str(data["package_sha256"]),
            **{
                name: str(data[name])
                for name in _CONTENT_FIELDS
                if name not in {"approved_answers", "instructions"}
            },
        )

    def safe_view(self) -> dict[str, Any]:
        """Visao imprimivel: sem respostas e sem o caminho absoluto do curriculo.

        O pacote completo fica no diretorio privado de artifacts. O que circula
        em terminal, log ou mensagem nao carrega o que o humano respondeu — nem o
        diretorio pessoal de quem rodou o agente.
        """
        return {
            "package_id": self.package_id,
            "package_sha256": self.package_sha256,
            "application_id": self.application_id,
            "job_id": self.job_id,
            "provider": self.provider,
            "reason_token": self.reason_token,
            "challenge_session_id": self.challenge_session_id,
            "destination": self.destination,
            "page_url": self.page_url,
            "company": self.company,
            "title": self.title,
            "resume_sha256": self.resume_sha256,
            "answers_fingerprint": self.answers_fingerprint,
            "approved_answers_count": len(self.approved_answers),
            "continuation": self.continuation,
            "instructions": list(self.instructions),
            "package_path": self.package_path,
            "created_at": self.created_at,
        }


def approved_answers(form: ApplicationForm) -> tuple[HandoffAnswer, ...]:
    """Somente o que estava APROVADO. Nada e inferido, completado ou resumido."""
    rows: dict[str, HandoffAnswer] = {}
    for field in form.fields:
        answer = field.answer
        if answer is None or not answer.approved:
            continue
        text = str(answer.answer or "").strip()
        if not text:
            continue
        key = str(answer.question_key or field.key)
        rows.setdefault(key, HandoffAnswer(key, str(answer.question or field.label), text))
    return tuple(rows[key] for key in sorted(rows))


def _package_id(
    *,
    application_id: str,
    job_id: str,
    provider: str,
    reason_token: str,
    challenge_session_id: str,
    destination: str,
    page_url: str,
    resume_sha256: str,
    answers_fingerprint: str,
) -> str:
    """Endereco derivado do CONTEUDO: material identico e o MESMO pacote."""
    payload = {
        "application_id": application_id,
        "job_id": job_id,
        "provider": provider,
        "reason_token": reason_token,
        "challenge_session_id": challenge_session_id,
        "destination": destination,
        "page_url": page_url,
        "resume_sha256": resume_sha256,
        "answers_fingerprint": answers_fingerprint,
    }
    return "hpkg-" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:24]


def _form_from_dict(data: Mapping[str, Any] | None) -> ApplicationForm:
    if not data:
        raise HandoffError("handoff requires the persisted application form")
    form = context_from_dict({"form": dict(data)}).form
    if form is None:
        raise HandoffError("persisted application form could not be read back")
    return form


class HumanHandoffService:
    """Monta o pacote e so entao move a Application (JSA-CG-016)."""

    def __init__(self, database: Database, artifacts_dir: str | Path):
        self.database = database
        self.artifacts_dir = Path(artifacts_dir)

    # -- leitura ---------------------------------------------------------------

    def load_package(self, package_id: str) -> HumanHandoffPackage:
        stored = self.database.get_handoff_package(package_id)
        if stored is None:
            raise HandoffError(f"handoff package not found: {package_id}")
        return HumanHandoffPackage.from_dict(stored)

    def recorded_challenge_handoff(self, application: Application) -> dict[str, Any]:
        """Handoff neutro reconstruido da recusa registrada.

        E o caminho de quem NAO tem mais o processo vivo (o CLI). Sem
        proveniencia gravada nao ha o que reconstruir: inventar "captcha" a
        partir do estado seria afirmar um fato que ninguem observou.
        """
        attempt = self._recorded_rejection(application)
        observed = (attempt.evidence or {}).get("challenge")
        observed = observed if isinstance(observed, dict) else {}
        provider = str(observed.get("provider", ""))
        reason_token = str(observed.get("reason_token", ""))
        if not provider or not reason_token:
            raise HandoffError(
                "the recorded rejection has no challenge provenance; "
                "rerun the live flow so the observation is recorded"
            )
        job = self.database.get_job(application.job_id)
        page_url = self._apply_url(attempt.provider, job) if job is not None else ""
        handoff = recorded_handoff(
            provider=provider,
            reason_token=reason_token,
            session_id=str(observed.get("session_id", "")),
            page_url=page_url,
        )
        if not handoff:
            raise HandoffError(f"recorded challenge reason is not reconstructible: {reason_token}")
        return handoff

    # -- a operacao ------------------------------------------------------------

    def prepare_handoff(
        self,
        application_id: str,
        challenge_handoff: Mapping[str, Any] | None = None,
    ) -> HumanHandoffPackage:
        """Monta, valida e persiste o pacote, e so entao inicia o handoff.

        Sem ``challenge_handoff`` o handoff e reconstruido do que ficou gravado
        (o caso do CLI). Com ele, o objeto vem do ``ChallengeOutcome.handoff``
        observado ao vivo.
        """
        application = self.database.get_application(application_id)
        if application is None:
            raise HandoffError(f"application not found: {application_id}")
        if application.state is ApplicationState.HANDOFF_IN_PROGRESS:
            # Repetir a LEITURA do handoff e seguro; criar um segundo pacote para
            # o mesmo handoff seria uma segunda verdade.
            recorded = self.database.list_handoff_packages(application_id)
            if not recorded:
                raise HandoffError("application is in HANDOFF_IN_PROGRESS without a recorded package")
            return HumanHandoffPackage.from_dict(recorded[-1])
        if application.state is not ApplicationState.NEEDS_HUMAN_CAPTCHA:
            raise HandoffError(f"handoff requires NEEDS_HUMAN_CAPTCHA, got {application.state.value}")
        if challenge_handoff is None:
            challenge_handoff = self.recorded_challenge_handoff(application)
        package = self.build_package(application, challenge_handoff)
        created = self._write_bundle(package)
        try:
            ApplicationService(self.database).start_human_handoff(application_id, package=package)
        except Exception:
            # Nada de pacote orfao: se o handoff nao comecou, o bundle tambem nao
            # fica. O estado segue NEEDS_HUMAN_CAPTCHA.
            if created:
                shutil.rmtree(self._bundle_dir(package), ignore_errors=True)
            raise
        return package

    def build_package(
        self,
        application: Application,
        challenge_handoff: Mapping[str, Any],
    ) -> HumanHandoffPackage:
        """Constroi e valida em memoria. Nao escreve nada — isso e o `prepare_handoff`."""
        if application.state is not ApplicationState.NEEDS_HUMAN_CAPTCHA:
            raise HandoffError(f"handoff requires NEEDS_HUMAN_CAPTCHA, got {application.state.value}")
        job = self.database.get_job(application.job_id)
        if job is None:
            raise HandoffError(f"job not found: {application.job_id}")
        attempt = self._recorded_rejection(application)
        recorded = (attempt.evidence or {}).get("challenge")
        recorded = recorded if isinstance(recorded, dict) else {}

        provider = str(challenge_handoff.get("provider") or recorded.get("provider") or "")
        reason_token = str(challenge_handoff.get("reason_token") or recorded.get("reason_token") or "")
        session_id = str(challenge_handoff.get("challenge_session_id") or recorded.get("session_id") or "")
        if not provider or not reason_token:
            raise HandoffError("handoff requires the challenge provider and reason observed at the rejection")
        # A observacao do desafio vem do challenge-guard; ela nao pode contradizer
        # o que ficou registrado na tentativa.
        for name, value in (("provider", provider), ("reason_token", reason_token)):
            stored = str(recorded.get(name, ""))
            if stored and stored != value:
                raise HandoffError(f"handoff {name} does not match the recorded rejection")

        snapshot = self.database.get_review_snapshot(application.id)
        if snapshot is None:
            raise HandoffError("handoff requires the reviewed submission snapshot")
        if snapshot.job_id != application.job_id:
            raise HandoffError("reviewed snapshot does not belong to this application")
        if snapshot.provider and snapshot.provider != attempt.provider:
            raise HandoffError("reviewed provider does not match the recorded rejection")
        destination = public_url(str(snapshot.destination or ""), "destination")

        resume_sha256 = self._approved_resume(application, str(snapshot.resume_sha256 or ""))
        answers, answers_fingerprint = self._approved_answers(application, str(snapshot.answers_fingerprint or ""))

        page_url = str(challenge_handoff.get("page_url") or "") or self._apply_url(attempt.provider, job)
        public_url(page_url or destination, "page_url")

        package_id = _package_id(
            application_id=application.id,
            job_id=application.job_id,
            provider=provider,
            reason_token=reason_token,
            challenge_session_id=session_id,
            destination=destination,
            page_url=page_url or destination,
            resume_sha256=resume_sha256,
            answers_fingerprint=answers_fingerprint,
        )
        # Material identico e o MESMO handoff: devolver o pacote ja gravado em vez
        # de reescreve-lo e o que mantem o bundle imutavel.
        stored = self.database.get_handoff_package(package_id)
        if stored is not None:
            return HumanHandoffPackage.from_dict(stored)

        bundle = Path(application.job_id) / "handoff" / package_id
        return HumanHandoffPackage.create(
            package_id=package_id,
            application_id=application.id,
            job_id=application.job_id,
            provider=provider,
            reason_token=reason_token,
            challenge_session_id=session_id,
            destination=destination,
            page_url=page_url or destination,
            company=str(job.company or ""),
            title=str(job.title or ""),
            resume_path=str(bundle / "resume.pdf"),
            resume_sha256=resume_sha256,
            answers_fingerprint=answers_fingerprint,
            approved_answers=answers,
            continuation=str(challenge_handoff.get("continuation") or "manual_final"),
            instructions=tuple(str(item) for item in (challenge_handoff.get("instructions") or ())),
            package_path=str(bundle / "package.json"),
            created_at=now_iso(),
        )

    # -- persistencia do bundle ------------------------------------------------

    def _bundle_dir(self, package: HumanHandoffPackage) -> Path:
        root = self.artifacts_dir.resolve()
        bundle = (root / package.package_path).parent
        try:
            bundle.relative_to(root)
        except ValueError as exc:
            raise HandoffError("handoff package path escapes the controlled artifact root") from exc
        return bundle

    def _write_bundle(self, package: HumanHandoffPackage) -> bool:
        """Escreve `package.json` e a copia do curriculo. Diz se CRIOU o bundle.

        A copia e o que torna o pacote autocontido: se o curriculo for regerado
        depois, o pacote antigo continua sendo exatamente o que foi aprovado.
        """
        root = self.artifacts_dir.resolve()
        bundle = self._bundle_dir(package)
        created = not bundle.exists()
        try:
            bundle.mkdir(parents=True, exist_ok=True)
            target = root / package.resume_path
            if not target.exists():
                shutil.copyfile(root / package.job_id / "resume.pdf", target)
            written = hashlib.sha256(target.read_bytes()).hexdigest()
            if written != package.resume_sha256:
                raise HandoffError("handoff resume copy does not match the approved hash")
            (bundle / "package.json").write_text(
                json.dumps(package.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except Exception:
            if created:
                shutil.rmtree(bundle, ignore_errors=True)
            raise
        return created

    # -- fontes do material aprovado -------------------------------------------

    def _recorded_rejection(self, application: Application):
        attempts = self.database.list_submission_attempts(application.id)
        last = attempts[-1] if attempts else None
        if last is None or last.status != ApplicationState.NEEDS_HUMAN_CAPTCHA.value:
            raise HandoffError("handoff requires a recorded provider rejection (no submission attempt)")
        return last

    def _approved_resume(self, application: Application, reviewed_sha256: str) -> str:
        """O curriculo aprovado, conferido contra o hash que ficou registrado."""
        root = self.artifacts_dir.resolve()
        source = root / application.job_id / "resume.pdf"
        recorded = str(application.context.get("resume_sha256", ""))
        if not recorded:
            raise HandoffError("handoff requires the resume hash recorded at preparation time")
        if reviewed_sha256 and reviewed_sha256 != recorded:
            raise HandoffError("reviewed resume does not match the resume that was prepared")
        if not source.is_file():
            raise HandoffError("approved resume artifact is missing")
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual != recorded:
            raise HandoffError("resume artifact changed after it was approved")
        return actual

    def _approved_answers(
        self, application: Application, reviewed_fingerprint: str
    ) -> tuple[tuple[HandoffAnswer, ...], str]:
        """As respostas aprovadas, conferidas contra o snapshot do review."""
        form = _form_from_dict(self.database.get_application_form(application.id))
        current = compute_answers_fingerprint(form)
        if not reviewed_fingerprint:
            raise HandoffError("handoff requires the answers fingerprint recorded at review time")
        if current != reviewed_fingerprint:
            raise HandoffError("approved answers changed after the review snapshot")
        return approved_answers(form), current

    def _apply_url(self, provider: str, job: Job) -> str:
        if job is None or not job.url:
            return ""
        try:
            return provider_apply_url(provider, job.url)
        except Exception:  # pragma: no cover - provider sem perfil declarado
            return ""
