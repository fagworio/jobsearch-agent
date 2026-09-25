"""Estratégias concretas de resolução.

Este pacote é intencionalmente vazio na Fase 0: nenhuma estratégia concreta é
registrada por padrão, e nada aqui interage com browser.

Fase 5 adicionará a primeira estratégia (`HumanHandoffStrategy`). As demais
entram depois, cada uma com feature flag própria e com a `CapabilityMatrix`
atualizada — nunca por padrão ligadas.
"""

from .base import NullStrategy, StrategyRegistry

__all__ = ["NullStrategy", "StrategyRegistry"]
