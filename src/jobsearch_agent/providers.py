"""Contrato declarativo por provider de ATS.

Cada ATS difere em três pontos: quais hosts a página precisa carregar, qual é o
endpoint que recebe a candidatura e como se reconhece o controle final e a
confirmação. Reunir isso num perfil evita espalhar `if provider == ...` pelo
pipeline e deixa explícito o que cada provider exige.

Descobertas por inspeção real das páginas:

- **Greenhouse**: SPA. O formulário é montado no cliente e o POST é
  ``application/json`` com ``g-recaptcha-enterprise-token``, ``request_token`` e
  ``csrfToken``. O currículo sobe antes, por POST para o storage do board.
- **Lever**: formulário HTML clássico em ``<job-url>/apply``, com
  ``action`` apontando para o próprio ``/apply`` e ``method=post``.
- **Ashby**: SPA GraphQL. O próprio formulário é carregado por POST para a API,
  então nem a inspeção dispensa escrita — ver ``form_loaded_by_api_write``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


class ProviderError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderProfile:
    provider: str
    #: Hosts de terceiros que a página carrega, liberados apenas para leitura.
    resource_hosts: tuple[str, ...] = ()
    #: Origens para onde a aplicação envia o currículo (POST), com permissão
    #: one-shot durante uma submissão autorizada.
    upload_write_origins: tuple[str, ...] = ()
    #: Origem e caminho do POST de candidatura.
    submit_origin: str = ""
    submit_path_pattern: str = ""
    submit_method: str = "POST"
    #: Rótulos aceitos para o controle final, do mais específico ao mais genérico.
    submit_control_names: tuple[str, ...] = ()
    #: Marcadores textuais de que a página chegou à confirmação.
    confirmation_markers: tuple[str, ...] = ()
    #: Sufixo que transforma a URL da vaga na URL do formulário.
    apply_path_suffix: str = ""
    #: True quando o próprio formulário só existe após uma escrita na API.
    form_loaded_by_api_write: bool = False
    notes: str = ""


_FALLBACK_CONFIRMATION = (
    "thank you for applying",
    "thanks for applying",
    "application submitted",
    "application has been submitted",
    "we received your application",
    "your application was submitted",
)

PROFILES: dict[str, ProviderProfile] = {
    "greenhouse": ProviderProfile(
        provider="greenhouse",
        resource_hosts=(
            "www.recaptcha.net",
            "recaptcha.net",
            "www.google.com",
            "apis.google.com",
            "accounts.google.com",
            "fonts.googleapis.com",
            "fonts.gstatic.com",
            "recruiting.cdn.greenhouse.io",
            "my.greenhouse.io",
            "www.dropbox.com",
            "*.s3.amazonaws.com",
        ),
        upload_write_origins=("*.s3.amazonaws.com",),
        submit_origin="https://boards.greenhouse.io",
        submit_path_pattern=r"^/[^/]+/jobs/[^/]+/?$",
        submit_control_names=("Submit application", "Submit Application", "Submit"),
        confirmation_markers=_FALLBACK_CONFIRMATION,
        notes="SPA; currículo sobe por POST para o storage antes do submit.",
    ),
    "lever": ProviderProfile(
        provider="lever",
        resource_hosts=(
            "cdn.lever.co",
            "*.lever.co",
            "fonts.googleapis.com",
            "fonts.gstatic.com",
            "www.gstatic.com",
            "*.s3.amazonaws.com",
        ),
        upload_write_origins=("*.s3.amazonaws.com", "*.lever.co"),
        submit_origin="https://jobs.lever.co",
        submit_path_pattern=r"^/[^/]+/[0-9a-fA-F-]{8,}/apply/?$",
        submit_control_names=("SUBMIT APPLICATION", "Submit Application", "Submit application"),
        # Nunca um marcador generico como "posting": a pagina de uma vaga
        # contem "job posting" e um POST 2xx + esse texto marcaria SUBMITTED.
        confirmation_markers=(
            "thank you for applying",
            "thanks for applying",
            "application submitted",
            "application received",
        ),
        apply_path_suffix="/apply",
        notes="Formulário HTML clássico; action aponta para o próprio /apply.",
    ),
    "ashby": ProviderProfile(
        provider="ashby",
        resource_hosts=(
            "cdn.ashbyprd.com",
            "*.ashbyprd.com",
            "*.ashbyhq.com",
            "fonts.googleapis.com",
            "fonts.gstatic.com",
            "www.gstatic.com",
            "*.s3.amazonaws.com",
        ),
        upload_write_origins=("*.s3.amazonaws.com",),
        # Sem politica de submit declarada de proposito: enquanto o formulario
        # depender de um POST na API para existir, nao ha endpoint publico que
        # possamos autorizar com seguranca.
        submit_origin="",
        submit_path_pattern="",
        submit_control_names=("Submit Application", "Submit application", "Submit"),
        confirmation_markers=_FALLBACK_CONFIRMATION,
        apply_path_suffix="/application",
        form_loaded_by_api_write=True,
        notes=(
            "SPA GraphQL: o formulário é carregado por POST para a API, então a "
            "inspeção também exige uma escrita autorizada."
        ),
    ),
}


#: Sufixos de host que identificam o provider sem depender de um adapter.
PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "ashby": ("ashbyhq.com",),
}


def provider_for_url(url: str) -> str:
    """Provider identificado pelo host, ou "" se desconhecido."""
    host = (urlsplit(url).hostname or "").casefold()
    if not host:
        return ""
    for provider, suffixes in PROVIDER_HOSTS.items():
        if any(host == suffix or host.endswith("." + suffix) for suffix in suffixes):
            return provider
    return ""


def supported_providers() -> list[str]:
    return sorted(PROFILES)


def profile_for(provider: str) -> ProviderProfile:
    profile = PROFILES.get(str(provider).casefold().strip())
    if profile is None:
        raise ProviderError(f"no provider profile for: {provider}")
    return profile


def apply_url(provider: str, job_url: str) -> str:
    """URL do formulário de candidatura para a vaga.

    Usa ``urlsplit`` para nao corromper query string ou fragmento:
    ``.../uuid?lever-source=linkedin`` vira ``.../uuid/apply?lever-source=linkedin``.
    """
    profile = profile_for(provider)
    if not profile.apply_path_suffix:
        return job_url
    parts = urlsplit(job_url)
    path = parts.path.rstrip("/")
    if path.casefold().endswith(profile.apply_path_suffix):
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
    return urlunsplit(
        (parts.scheme, parts.netloc, path + profile.apply_path_suffix, parts.query, parts.fragment)
    )


def submit_destination(
    provider: str,
    job_url: str,
    board: str,
    external_id: str,
    form_action: str = "",
) -> str:
    """Endpoint que recebe o POST da candidatura.

    Lever publica o destino no próprio ``action`` do formulário (a URL
    ``/apply``); Greenhouse usa um caminho separado em ``boards.greenhouse.io``.
    """
    profile = profile_for(provider)
    if provider == "greenhouse":
        if not board or not external_id:
            raise ProviderError("greenhouse submission requires board and job id")
        return f"{profile.submit_origin}/{board}/jobs/{external_id}"
    if provider == "lever":
        # O formulario declara o destino exato no `action`; so reconstruimos
        # quando ele nao vier.
        return form_action or apply_url(provider, job_url)
    raise ProviderError(f"no public submit endpoint known for provider: {provider}")
