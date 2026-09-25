"""Capability matrix.

Define, para cada par (provider, challenge_type), qual é o tratamento esperado.
É **dado**, não código: alterar esta matriz é mudança de política, e deve ser
revisada como tal.

Regra dura: par não listado cai em `UNSUPPORTED` (fail-safe). Não existe default
permissivo, e `lookup()` nunca devolve `None`.

Chaves canônicas vêm do `challenge-guard` v0.1.0:

    provider: unknown · recaptcha · recaptcha_enterprise · hcaptcha · turnstile · generic
    tipo:     invisible · checkbox · image_selection · text · math · puzzle ·
              risk_assessment · unknown

`email`/`sms` e os tipos `verification`/`mfa`/`managed` NÃO existem no guard
hoje. Eles ficam declarados aqui porque são o alvo do handoff de verificação
(Fase 8) e porque a matriz é o lugar onde essa política vive — mas nenhuma
observação atual consegue produzir essas chaves, e por isso elas começam como
`HUMAN_REQUIRED` (nunca como `SUPPORTED`).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping

from .types import (
    CapabilityKey,
    CapabilityStatus,
    ChallengeTypeKey,
    ProviderKey,
    enum_value,
)

# ---------------------------------------------------------------------------
# Nomes canônicos
# ---------------------------------------------------------------------------

PROVIDER_UNKNOWN: Final[ProviderKey] = "unknown"
PROVIDER_RECAPTCHA: Final[ProviderKey] = "recaptcha"
PROVIDER_RECAPTCHA_ENTERPRISE: Final[ProviderKey] = "recaptcha_enterprise"
PROVIDER_HCAPTCHA: Final[ProviderKey] = "hcaptcha"
PROVIDER_TURNSTILE: Final[ProviderKey] = "turnstile"
PROVIDER_EMAIL: Final[ProviderKey] = "email"
PROVIDER_SMS: Final[ProviderKey] = "sms"

TYPE_CHECKBOX: Final[ChallengeTypeKey] = "checkbox"
TYPE_INVISIBLE: Final[ChallengeTypeKey] = "invisible"
TYPE_IMAGE_SELECTION: Final[ChallengeTypeKey] = "image_selection"
TYPE_RISK_ASSESSMENT: Final[ChallengeTypeKey] = "risk_assessment"
TYPE_MANAGED: Final[ChallengeTypeKey] = "managed"
TYPE_VERIFICATION: Final[ChallengeTypeKey] = "verification"
TYPE_MFA: Final[ChallengeTypeKey] = "mfa"

#: Apelidos aceitos na consulta. O rascunho da Fase 0 usava `image`; o guard
#: chama isso de `image_selection`. Aceitar os dois evita uma tabela morta por
#: causa de vocabulário.
PROVIDER_ALIASES: Mapping[str, ProviderKey] = MappingProxyType(
    {
        "recaptcha_enterprise": PROVIDER_RECAPTCHA_ENTERPRISE,
        "recaptcha-enterprise": PROVIDER_RECAPTCHA_ENTERPRISE,
        "h-captcha": PROVIDER_HCAPTCHA,
        "cloudflare_turnstile": PROVIDER_TURNSTILE,
    }
)

TYPE_ALIASES: Mapping[str, ChallengeTypeKey] = MappingProxyType(
    {
        "image": TYPE_IMAGE_SELECTION,
        "image_selection": TYPE_IMAGE_SELECTION,
        "imageselection": TYPE_IMAGE_SELECTION,
        "risk": TYPE_RISK_ASSESSMENT,
        "risk_assessment": TYPE_RISK_ASSESSMENT,
    }
)

# ---------------------------------------------------------------------------
# Matriz — imutável por construção
# ---------------------------------------------------------------------------

_CAPABILITY_MATRIX: dict[CapabilityKey, CapabilityStatus] = {
    # Checkbox e invisible são interações de baixo risco (um clique humano ou um
    # widget que se resolve sozinho). Imagem continua sendo de pessoa.
    (PROVIDER_RECAPTCHA, TYPE_CHECKBOX): CapabilityStatus.SUPPORTED,
    (PROVIDER_RECAPTCHA, TYPE_INVISIBLE): CapabilityStatus.SUPPORTED,
    (PROVIDER_RECAPTCHA, TYPE_IMAGE_SELECTION): CapabilityStatus.HUMAN_REQUIRED,
    # O board da Fueled observou `recaptcha_enterprise`: mesma família, mesmo
    # tratamento — declarado explicitamente para não cair no fail-safe por
    # causa de um nome diferente.
    (PROVIDER_RECAPTCHA_ENTERPRISE, TYPE_CHECKBOX): CapabilityStatus.SUPPORTED,
    (PROVIDER_RECAPTCHA_ENTERPRISE, TYPE_INVISIBLE): CapabilityStatus.SUPPORTED,
    (PROVIDER_RECAPTCHA_ENTERPRISE, TYPE_IMAGE_SELECTION): CapabilityStatus.HUMAN_REQUIRED,
    (PROVIDER_HCAPTCHA, TYPE_CHECKBOX): CapabilityStatus.SUPPORTED,
    (PROVIDER_HCAPTCHA, TYPE_IMAGE_SELECTION): CapabilityStatus.UNSUPPORTED,
    (PROVIDER_TURNSTILE, TYPE_MANAGED): CapabilityStatus.SUPPORTED,
    (PROVIDER_TURNSTILE, TYPE_INVISIBLE): CapabilityStatus.SUPPORTED,
    (PROVIDER_TURNSTILE, TYPE_RISK_ASSESSMENT): CapabilityStatus.HUMAN_REQUIRED,
    # Canais que o guard ainda não emite: reservados, e deliberadamente de
    # pessoa — um código de verificação é credencial, e o agente não a usa.
    (PROVIDER_EMAIL, TYPE_VERIFICATION): CapabilityStatus.HUMAN_REQUIRED,
    (PROVIDER_SMS, TYPE_MFA): CapabilityStatus.HUMAN_REQUIRED,
}

#: Versão pública imutável.
CAPABILITY_MATRIX: Mapping[CapabilityKey, CapabilityStatus] = MappingProxyType(_CAPABILITY_MATRIX)


class CapabilityMatrix:
    """Wrapper de consulta sobre a matriz estática.

    Não expõe a matriz crua. Fornece `lookup()` com fail-safe para
    `UNSUPPORTED`. Pode receber outra matriz em testes, sem herança.
    """

    def __init__(self, matrix: Mapping[CapabilityKey, CapabilityStatus] | None = None) -> None:
        self._matrix: Mapping[CapabilityKey, CapabilityStatus] = (
            matrix if matrix is not None else CAPABILITY_MATRIX
        )

    @staticmethod
    def _normalise(provider: object, challenge_type: object) -> CapabilityKey:
        provider_key = enum_value(provider).strip().casefold()
        type_key = enum_value(challenge_type).strip().casefold()
        return (
            PROVIDER_ALIASES.get(provider_key, provider_key),
            TYPE_ALIASES.get(type_key, type_key),
        )

    def lookup(self, provider: object, challenge_type: object) -> CapabilityStatus:
        """Status declarado para o par. Desconhecido → `UNSUPPORTED`."""
        return self._matrix.get(self._normalise(provider, challenge_type), CapabilityStatus.UNSUPPORTED)

    def is_supported(self, provider: object, challenge_type: object) -> bool:
        return self.lookup(provider, challenge_type) == CapabilityStatus.SUPPORTED

    def known_providers(self) -> frozenset[ProviderKey]:
        return frozenset(provider for (provider, _type) in self._matrix)

    def entries(self) -> Mapping[CapabilityKey, CapabilityStatus]:
        return self._matrix
