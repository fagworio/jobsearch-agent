"""Nomes canônicos dos eventos de journal da resolução de challenge.

Constantes apenas: a Fase 0 **não emite** nada. Elas existem para que a Fase 1
emita eventos com nome estável e para que o journal seja legível por máquina
sem strings soltas espalhadas pelo código.

O redator de `jobsearch_agent.observability` já remove qualquer campo cujo nome
pareça sensível (`token`, `cookie`, `secret`, ...). Ainda assim, o payload
correto é a projeção de `ChallengeProvenance.as_journal()` — proveniência sem
segredo, e nada além dela.
"""

from __future__ import annotations

from typing import Final

#: O guard detectou um challenge (antes de qualquer tentativa).
CHALLENGE_DETECTED: Final[str] = "challenge_detected"

#: O orquestrador começou a rodar para esta sessão.
CHALLENGE_RESOLUTION_STARTED: Final[str] = "challenge_resolution_started"

#: Resultado de UM round de resolução.
CHALLENGE_RESOLUTION_RESULT: Final[str] = "challenge_resolution_result"

#: Veredito do validador após um round.
CHALLENGE_VALIDATION_RESULT: Final[str] = "challenge_validation_result"

#: Início e fim de cada round (para medir tempo por round).
CHALLENGE_ROUND_STARTED: Final[str] = "challenge_round_started"
CHALLENGE_ROUND_FINISHED: Final[str] = "challenge_round_finished"

#: Fim da orquestração, com a proveniência agregada.
CHALLENGE_ORCHESTRATION_FINISHED: Final[str] = "challenge_orchestration_finished"

#: Quantas escritas autorizadas foram consumidas na submissão (0 ou 1, nunca 2).
SUBMISSION_WRITE_COUNT: Final[str] = "submission_write_count"

#: Estado final da Application, para fechar a leitura do journal.
FINAL_STATE: Final[str] = "final_state"

#: Ordem esperada no journal de uma orquestração completa. Serve de contrato
#: para a Fase 7 (observabilidade) e para os testes da Fase 1.
ORCHESTRATION_SEQUENCE: Final[tuple[str, ...]] = (
    CHALLENGE_DETECTED,
    CHALLENGE_RESOLUTION_STARTED,
    CHALLENGE_ROUND_STARTED,
    CHALLENGE_RESOLUTION_RESULT,
    CHALLENGE_VALIDATION_RESULT,
    CHALLENGE_ROUND_FINISHED,
    CHALLENGE_ORCHESTRATION_FINISHED,
    SUBMISSION_WRITE_COUNT,
    FINAL_STATE,
)

__all__ = [
    "CHALLENGE_DETECTED",
    "CHALLENGE_RESOLUTION_STARTED",
    "CHALLENGE_RESOLUTION_RESULT",
    "CHALLENGE_VALIDATION_RESULT",
    "CHALLENGE_ROUND_STARTED",
    "CHALLENGE_ROUND_FINISHED",
    "CHALLENGE_ORCHESTRATION_FINISHED",
    "SUBMISSION_WRITE_COUNT",
    "FINAL_STATE",
    "ORCHESTRATION_SEQUENCE",
]
