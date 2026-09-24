"""Contrato declarativo por provider de ATS.

Cada ATS difere em três pontos: quais hosts a página precisa carregar, qual é o
endpoint que recebe a candidatura e como se reconhece o controle final e a
confirmação. Reunir isso num perfil evita espalhar `if provider == ...` pelo
pipeline e deixa explícito o que cada provider exige.

Descobertas por inspeção real das páginas:

- **Greenhouse**: SPA. O formulário é montado no cliente e o POST é
  ``application/json`` com tokens efemeros e ``request_token`` e
  ``csrfToken``. O currículo sobe antes, por POST para o storage do board.
- **Lever**: formulário HTML clássico em ``<job-url>/apply``, com
  ``action`` apontando para o próprio ``/apply`` e ``method=post``.
- **Ashby**: SPA GraphQL. O próprio formulário é carregado por POST para a API,
  então nem a inspeção dispensa escrita — ver ``form_loaded_by_api_write``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit


class ProviderError(ValueError):
    pass


#: Prefixo que marca um valor de binding como chave de contexto do job.
_CONTEXT_PREFIX = "$"


@dataclass(frozen=True)
class InspectionOperation:
    """Operação GraphQL read-only que a descoberta do formulário executa.

    ``bindings`` liga o nome da variável ao que ela PODE valer: um valor
    prefixado com ``$`` vem do contexto do job (``$board``, ``$external_id``);
    qualquer outro é literal. É esse vínculo que prende a requisição à vaga
    corrente — sem ele, um permit para o endpoint autorizaria consultar
    qualquer organização ou qualquer vaga.
    """

    name: str
    path: str
    bindings: tuple[tuple[str, str], ...]
    #: Variaveis que a operacao aceita mas nao exige. O SPA do Ashby chama
    #: ApiOrganizationFromHostedJobsPageName com e sem `searchContext`; exigir
    #: presenca recusaria uma chamada legitima da propria operacao permitida.
    #: Se presentes, ainda precisam bater com o valor declarado.
    optional_bindings: tuple[tuple[str, str], ...] = ()

    def _resolve(self, pairs: tuple[tuple[str, str], ...], context: dict[str, str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for variable, source in pairs:
            if source.startswith(_CONTEXT_PREFIX):
                key = source[len(_CONTEXT_PREFIX):]
                value = str(context.get(key, ""))
                if not value:
                    raise ProviderError(f"inspection operation {self.name} requires context key: {key}")
                resolved[variable] = value
            else:
                resolved[variable] = source
        return resolved

    def resolved_variables(self, context: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        """(obrigatorias, opcionais) ja resolvidas contra o contexto do job."""
        required = self._resolve(self.bindings, context)
        optional = self._resolve(self.optional_bindings, context)
        if not required:
            raise ProviderError(f"inspection operation {self.name} declares no bindings")
        overlap = set(required) & set(optional)
        if overlap:
            raise ProviderError(f"inspection operation {self.name} binds twice: {sorted(overlap)}")
        return required, optional


@dataclass(frozen=True)
class ProviderProfile:
    provider: str
    #: Hosts de terceiros que a página carrega, liberados apenas para leitura.
    resource_hosts: tuple[str, ...] = ()
    #: Origens para onde a aplicação envia o currículo (POST), com permissão
    #: one-shot durante uma submissão autorizada.
    upload_write_origins: tuple[str, ...] = ()
    #: Caminho aceito em cada origem de upload, no formato (origem, regex).
    #: Origens ausentes usam ``^/.*$``. Restringir por caminho impede que um
    #: POST de infraestrutura (ex.: o desafio do Cloudflare) consuma o
    #: orçamento da subida do currículo e evita que a permissão de upload
    #: cubra o endpoint de candidatura, que no Lever fica na mesma origem.
    upload_write_paths: tuple[tuple[str, str], ...] = ()
    #: Origem e caminho do POST de candidatura. `submit_origin` e a origem
    #: canonica (e a que forma o destino quando a vaga nao diz outra coisa);
    #: `submit_origins` sao origens ADICIONAIS do MESMO provider que o POST pode
    #: usar legitimamente. O Greenhouse tem dois hosts de board (o legado
    #: `boards.greenhouse.io` e o SPA `job-boards.greenhouse.io`) e a submissao no
    #: SPA sai da propria origem da pagina — recusar isso travava a candidatura
    #: real depois de o formulario estar preenchido (achado na vaga da Fueled).
    submit_origin: str = ""
    submit_origins: tuple[str, ...] = ()
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
    #: Endpoint e operações GraphQL read-only necessárias para revelar o
    #: formulário. Sem isso, `form_loaded_by_api_write` torna o board
    #: inalcançável: a inspeção não teria como buscar o schema.
    inspection_origin: str = ""
    inspection_path: str = ""
    inspection_method: str = "POST"
    inspection_operations: tuple = ()
    #: Quantas vezes cada operação pode ser pedida (retry do SPA é normal).
    inspection_max_requests: int = 3
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
        submit_origins=("https://job-boards.greenhouse.io",),
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
        # O Lever sobe o curriculo por POST em /parseResume, na MESMA origem do
        # endpoint de candidatura. Sem esta restricao o permit de upload (que e
        # um curinga de caminho) autorizaria o proprio submit, e um POST de
        # infraestrutura do Cloudflare (/cdn-cgi/challenge-platform/...) gastava
        # o unico credito e o curriculo nunca subia.
        upload_write_paths=(("*.lever.co", r"^/parseResume$"),),
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
    "workable": ProviderProfile(
        provider="workable",
        resource_hosts=(
            "*.workable.com",
            "workable.com",
            "fonts.googleapis.com",
            "fonts.gstatic.com",
            "challenges.cloudflare.com",
            "*.cloudflare.com",
            "cdnjs.cloudflare.com",
            "*.cloudfront.net",
        ),
        # NAO existe upload pre-submit neste board: o multipart para a propria
        # API E a submissao. Declarar o endpoint de candidatura como "upload"
        # armaria uma permissao de escrita ANTES de existir intent autorizada —
        # exatamente o que a separacao entre operacao de upload e operacao de
        # submissao evita. Aqui o orcamento de escrita e so o da submissao.
        upload_write_origins=(),
        upload_write_paths=(),
        submit_origin="https://apply.workable.com",
        submit_path_pattern=r"^/api/v[0-9]+/accounts/[^/]+/jobs/[^/]+/applications/?$",
        submit_control_names=("Submit application", "Submit Application", "Submit"),
        confirmation_markers=(
            "thank you for applying",
            "thanks for applying",
            "application submitted",
            "application received",
            "we have received your application",
        ),
        apply_path_suffix="/apply",
        notes=(
            "SPA React; o formulario vem de GET /api/v1/jobs/{id}/form e a "
            "candidatura sobe por POST multipart para a API do proprio board."
        ),
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
        inspection_origin="https://jobs.ashbyhq.com",
        inspection_path="/api/non-user-graphql",
        inspection_operations=(
            InspectionOperation(
                name="ApiJobPosting",
                path="/api/non-user-graphql",
                bindings=(
                    ("organizationHostedJobsPageName", "$board"),
                    ("jobPostingId", "$external_id"),
                ),
            ),
            InspectionOperation(
                name="ApiOrganizationFromHostedJobsPageName",
                path="/api/non-user-graphql",
                bindings=(("organizationHostedJobsPageName", "$board"),),
                optional_bindings=(("searchContext", "JobPosting"),),
            ),
        ),
        notes=(
            "SPA GraphQL: o formulário é montado a partir de POSTs read-only em "
            "/api/non-user-graphql (ApiJobPosting). A inspeção usa a boundary de "
            "inspeção, separada da de submissão."
        ),
    ),
}


#: Sufixos de host que identificam o provider sem depender de um adapter.
PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "ashby": ("ashbyhq.com",),
    "workable": ("workable.com",),
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


def board_from_url(provider: str, job_url: str) -> str:
    """Slug do board a partir da URL da vaga.

    Os três providers hospedados usam ``/<board>/<job>``; derivar da URL evita
    depender de ``job.company``, que é texto de exibição e pode divergir do slug.
    """
    profile_for(provider)
    segments = [segment for segment in urlsplit(job_url).path.split("/") if segment]
    return segments[0] if segments else ""


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
        # O destino tem de ser a origem em que a pagina REALMENTE vive: no SPA o
        # POST sai de `job-boards.greenhouse.io`, e um destino no host legado
        # seria recusado pela propria boundary.
        parsed = urlsplit(job_url)
        job_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        origin = job_origin if job_origin in {profile.submit_origin, *profile.submit_origins} else profile.submit_origin
        return f"{origin}/{board}/jobs/{external_id}"
    if provider == "lever":
        # O formulario declara o destino exato no `action`. Ele pode vir
        # relativo (`/apply`): sem resolver contra a URL da pagina, o valor
        # chegaria literal em `create_intent`, que exige URL absoluta.
        base = apply_url(provider, job_url)
        if form_action:
            resolved = urljoin(base, form_action)
            parts = urlsplit(resolved)
            if parts.scheme and parts.netloc:
                return resolved
        return base
    if provider == "workable":
        # A Workable identifica a vaga pelo par (conta, shortcode), e os dois
        # estao na URL: /{account}/j/{shortcode}. A URL e a fonte de verdade —
        # o `company` do registro pode vir vazio e o `external_id` e um hash
        # interno, nao o shortcode que a API espera.
        segments = [segment for segment in urlsplit(job_url).path.split("/") if segment]
        account = segments[0] if segments else board
        shortcode = segments[2] if len(segments) >= 3 and segments[1] == "j" else ""
        account = account or board
        shortcode = shortcode or external_id
        if not account or not shortcode:
            raise ProviderError("workable submission requires account and job shortcode")
        return f"{profile.submit_origin}/api/v1/accounts/{account}/jobs/{shortcode}/applications"
    raise ProviderError(f"no public submit endpoint known for provider: {provider}")
