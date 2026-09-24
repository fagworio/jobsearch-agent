"""Contrato acumulado da candidatura (JSA-LOOP-002).

Um formulario multi-step substitui o DOM a cada etapa. O `ApplicationForm`
continua significando **o que esta na tela AGORA** — mas a autorizacao final nao
pode cobrir so a ultima tela. Se ela cobrisse, teriamos:

    o browser preencheu tudo
    MAS o answers_fingerprint cobre apenas a ultima etapa
    E o ReviewSnapshot idem
    E a SubmissionIntent nao esta vinculada ao conjunto completo

Este modulo e o acumulador: cada passo entra com o formulario, o fingerprint e as
decisoes daquela etapa, e o contrato responde pelo TODO — quais respostas foram
dadas, onde, com que origem, e se duas etapas se contradizem.

Duas identidades de fingerprint, e elas nao sao a mesma coisa:

    form_fingerprint      a superficie FINAL onde o Submit acontece
    answers_fingerprint   TODAS as respostas aprovadas, de TODAS as etapas
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .models import ApplicationField, ApplicationForm, to_dict
from .submission import compute_answers_fingerprint


def is_answered(field: ApplicationField) -> bool:
    """Respondido por valor, por resposta OU por artefato anexado.

    O campo de curriculo nao tem `value` nem `answer`: o que ele tem e
    `attachment_path`. Sem contar o anexo, um formulario completo reporta o
    resume como pergunta sem resposta.
    """
    if field.value not in (None, ""):
        return True
    if field.answer is not None and field.answer.answer:
        return True
    return bool(str(getattr(field, "attachment_path", "") or "").strip())


def _value_of(field: ApplicationField) -> str:
    if field.value not in (None, ""):
        return str(field.value)
    if field.answer is not None and field.answer.answer:
        return str(field.answer.answer)
    return ""


@dataclass(frozen=True)
class ApplicationStep:
    """Um passo: o formulario que estava no DOM, com as decisoes daquela etapa."""

    index: int
    form_fingerprint: str
    url: str = ""
    provider: str = ""
    field_keys: tuple[str, ...] = ()
    #: field_key -> {status, source, reason, supported_by, step}
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "form_fingerprint": self.form_fingerprint,
            "url": self.url,
            "provider": self.provider,
            "field_keys": list(self.field_keys),
            "decisions": {key: dict(value) for key, value in self.decisions.items()},
        }


@dataclass(frozen=True)
class ContractConflict:
    """Duas etapas discordam sobre o mesmo campo. Nao se escolhe em silencio."""

    identity: str
    field_key: str
    first_index: int
    first_value: str
    later_index: int
    later_value: str

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


class ApplicationJourney:
    """O acumulador. O browser submete o formulario; isto autoriza e audita."""

    def __init__(self, *, provider: str = "", job_id: str = "") -> None:
        self.provider = provider
        self.job_id = job_id
        self._steps: list[ApplicationStep] = []
        self._fields: dict[str, ApplicationField] = {}
        self._first_seen: dict[str, int] = {}
        self._decisions: dict[str, dict[str, Any]] = {}
        self._conflicts: list[ContractConflict] = []

    # -- acumulo ---------------------------------------------------------------

    def identity(self, field: ApplicationField) -> str:
        """Identidade ESTAVEL entre etapas: provider + chave do campo.

        O indice da etapa nao entra: se entrasse, o mesmo campo em duas telas
        viraria duas identidades e a contradicao passaria despercebida.
        """
        provider = self.provider or "unknown"
        return f"{provider}|{field.key}"

    def add_step(
        self,
        *,
        index: int,
        form: ApplicationForm,
        form_fingerprint: str,
        url: str = "",
        decisions: dict[str, dict[str, Any]] | None = None,
    ) -> ApplicationStep:
        step_decisions: dict[str, dict[str, Any]] = {}
        for field in form.fields:
            identity = self.identity(field)
            record = dict((decisions or {}).get(field.key, {}))
            record["step"] = index
            step_decisions[field.key] = record
            existing = self._fields.get(identity)
            if existing is None:
                self._fields[identity] = field
                self._first_seen[identity] = index
            else:
                self._detect_conflict(identity, field, existing, index)
            # A decisao da PRIMEIRA aparicao e a que fica: acumular nao pode
            # apagar proveniencia anterior.
            self._decisions.setdefault(identity, record)
        step = ApplicationStep(
            index=index,
            form_fingerprint=form_fingerprint,
            url=url,
            provider=form.provider or self.provider,
            field_keys=tuple(field.key for field in form.fields),
            decisions=step_decisions,
        )
        self._steps.append(step)
        return step

    def _detect_conflict(
        self, identity: str, later: ApplicationField, first: ApplicationField, index: int
    ) -> None:
        first_value = _value_of(first)
        later_value = _value_of(later)
        if not first_value or not later_value or first_value == later_value:
            # Vazio depois nao contradiz nada; igual e idempotente.
            return
        if any(item.identity == identity for item in self._conflicts):
            return
        self._conflicts.append(
            ContractConflict(
                identity=identity,
                field_key=later.key,
                first_index=self._first_seen[identity],
                first_value=first_value,
                later_index=index,
                later_value=later_value,
            )
        )

    # -- consultas -------------------------------------------------------------

    @property
    def steps(self) -> tuple[ApplicationStep, ...]:
        return tuple(self._steps)

    @property
    def cycles(self) -> int:
        return len(self._steps)

    @property
    def steps_completed(self) -> int:
        """Avancos concluidos: a ultima tela inspecionada ainda nao foi avancada."""
        return max(len(self._steps) - 1, 0)

    @property
    def conflicts(self) -> tuple[ContractConflict, ...]:
        return tuple(self._conflicts)

    def resolved_fields(self) -> tuple[ApplicationField, ...]:
        """TODAS as respostas acumuladas, uma por identidade, na ordem de entrada."""
        return tuple(self._fields.values())

    def as_form(self) -> ApplicationForm:
        """Formulario sintetico com o contrato inteiro — para snapshot e auditoria.

        Nao reconstroi o POST: o browser continua submetendo o formulario real.
        """
        first = self._steps[0] if self._steps else None
        return ApplicationForm(
            form_id=f"journey-{self.job_id or 'application'}",
            provider=self.provider or (first.provider if first else "generic"),
            fields=list(self._fields.values()),
            source="application_journey",
            steps=[str(step.index) for step in self._steps],
        )

    def answers_fingerprint(self) -> str:
        """Fingerprint de TODAS as etapas.

        Reusa `compute_answers_fingerprint` sobre o formulario sintetico: numa
        candidatura de uma etapa o valor e identico ao de sempre, e numa
        multi-step ele cobre o contrato inteiro em vez da ultima tela.
        """
        return compute_answers_fingerprint(self.as_form())

    def decisions(self) -> dict[str, dict[str, Any]]:
        """Decisao por identidade de campo, com a etapa onde apareceu."""
        return {identity: dict(record) for identity, record in self._decisions.items()}

    def decisions_by_field(self) -> dict[str, dict[str, Any]]:
        """Decisao por `field_key`, para quem audita pelo nome do campo."""
        by_field: dict[str, dict[str, Any]] = {}
        for identity, record in self._decisions.items():
            field_key = identity.split("|", 1)[-1]
            by_field.setdefault(field_key, dict(record))
        return by_field

    def pending_required(self) -> tuple[str, ...]:
        """Perguntas obrigatorias sem resposta em QUALQUER etapa."""
        pending: list[str] = []
        for field in self._fields.values():
            if not field.required or is_answered(field):
                continue
            label = (field.label or field.key).strip()
            if label and label not in pending:
                pending.append(label)
        return tuple(pending)

    def answered_count(self) -> int:
        return sum(1 for field in self._fields.values() if is_answered(field))

    def to_dict(self) -> dict[str, Any]:
        """Resumo auditavel: etapas e decisoes, sem o texto das respostas."""
        return {
            "provider": self.provider,
            "job_id": self.job_id,
            "steps": [step.to_dict() for step in self._steps],
            "steps_completed": self.steps_completed,
            "answers_fingerprint": self.answers_fingerprint(),
            "answered": self.answered_count(),
            "conflicts": [item.to_dict() for item in self._conflicts],
        }
