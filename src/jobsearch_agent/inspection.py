"""Boundary de inspeção para boards que só revelam o formulário por POST.

Alguns ATS são SPAs que montam o formulário a partir de uma resposta de API. O
Ashby, por exemplo, busca o schema por ``POST /api/non-user-graphql``. Recusar
toda escrita na inspeção deixaria esses boards inalcançáveis; liberar "POST
durante a inspeção" seria abrir mão da boundary.

A distinção que este módulo formaliza:

    HTTP POST não implica semanticamente escrita. A autorização depende do
    propósito e do conteúdo da operação. Uma requisição POST de inspeção só é
    permitida quando for comprovadamente read-only, limitada ao provider,
    endpoint, operação e recurso esperados.

Isso é deliberadamente separado de ``AuthorizedWrite`` (upload, CAPTCHA,
submissão) e de ``SubmissionIntent``: misturar os dois contratos faria o suporte
a um provider novo enfraquecer a boundary que já funciona para os outros.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .providers import InspectionOperation, ProviderError, profile_for


#: Estágio único desta boundary. Um valor novo exige decisão explícita.
INSPECTION_STAGE_FORM_DISCOVERY = "FORM_DISCOVERY"

#: Tokens de auditoria. Conjunto fechado, como os de submissão: um valor novo
#: exige decisão explícita, porque é o que sobrevive à redação.
INSPECTION_REASON_TOKENS = frozenset(
    {
        "INSPECTION_OK",
        "INSPECTION_METHOD_MISMATCH",
        "INSPECTION_STAGE_MISMATCH",
        "INSPECTION_ORIGIN_MISMATCH",
        "INSPECTION_PATH_MISMATCH",
        "INSPECTION_BODY_UNPARSEABLE",
        "INSPECTION_OPERATION_NOT_ALLOWED",
        "INSPECTION_OPERATION_HINT_MISMATCH",
        "INSPECTION_NOT_A_QUERY",
        "INSPECTION_RESOURCE_MISMATCH",
        "INSPECTION_BUDGET_EXHAUSTED",
    }
)

class InspectionBoundaryError(ValueError):
    """Configuração de inspeção inválida — falha antes de qualquer rede."""


def _origin_of(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.hostname:
        return ""
    origin = f"{parsed.scheme.casefold()}://{parsed.hostname.casefold()}"
    if parsed.port is not None and parsed.port not in {80, 443}:
        origin += f":{parsed.port}"
    return origin


def _strip_graphql_noise(query: str) -> str | None:
    """Remove comentários e literais de string do texto GraphQL.

    Sem isso, um documento legítimo que apenas *menciona* ``mutation`` seria
    recusado à toa. Devolve ``None`` quando um literal fica aberto: nesse caso
    não dá para saber o que o resto do documento diz, e a resposta correta é
    recusar em vez de aceitar às cegas.
    """
    out: list[str] = []
    index = 0
    size = len(query)
    while index < size:
        char = query[index]
        if char == "#":
            while index < size and query[index] != "\n":
                index += 1
            continue
        if query.startswith('"""', index):
            end = query.find('"""', index + 3)
            if end == -1:
                return None
            index = end + 3
            out.append(" ")
            continue
        if char == '"':
            index += 1
            closed = False
            while index < size:
                if query[index] == "\\":
                    index += 2
                    continue
                if query[index] == '"':
                    index += 1
                    closed = True
                    break
                index += 1
            if not closed:
                return None
            out.append(" ")
            continue
        out.append(char)
        index += 1
    return "".join(out)


def graphql_query_is_read_only(query: str) -> bool:
    """True apenas quando o documento é comprovadamente uma consulta.

    Conservador de propósito: ``subscription`` e ``mutation`` recusam mesmo se
    aparecerem em posição inesperada, e exige-se a palavra ``query`` ou uma
    seleção anônima (``{``). Na dúvida, nega.
    """
    text = _strip_graphql_noise(str(query))
    if text is None or not text.strip():
        return False
    if re.search(r"\b(mutation|subscription)\b", text):
        return False
    return re.search(r"\bquery\b", text) is not None or text.lstrip().startswith("{")


def _json_object(body: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass(frozen=True)
class InspectionVerdict:
    covered: bool
    reason_token: str
    operation_name: str = ""

    def __post_init__(self) -> None:
        if self.reason_token not in INSPECTION_REASON_TOKENS:
            raise InspectionBoundaryError(f"unsupported inspection reason token: {self.reason_token}")


@dataclass(frozen=True)
class AuthorizedInspectionRequest:
    """Permissão para UMA requisição de inspeção, com orçamento próprio.

    Não é um ``AuthorizedWrite``: aquele significa "mutação permitida". Aqui a
    permissão é condicionada ao conteúdo — operação, tipo de documento
    GraphQL e vínculo com a vaga.
    """

    application_id: str
    provider: str
    origin: str
    path_pattern: str
    method: str
    operation_names: tuple[str, ...]
    expected_variables: tuple[tuple[str, str], ...]
    optional_variables: tuple[tuple[str, str], ...] = ()
    stage: str = INSPECTION_STAGE_FORM_DISCOVERY
    max_requests: int = 1

    def __post_init__(self) -> None:
        if self.max_requests < 1:
            raise InspectionBoundaryError("authorized inspection must allow at least one request")
        if not self.operation_names:
            raise InspectionBoundaryError("authorized inspection requires at least one operation name")
        if self.stage != INSPECTION_STAGE_FORM_DISCOVERY:
            raise InspectionBoundaryError(f"unsupported inspection stage: {self.stage}")

    def validate(self, *, method: str, url: str, body: str, stage: str) -> InspectionVerdict:
        """Decide se ESTA requisição está coberta. A ordem importa: cada passo é
        um portão, e o motivo devolvido é o do primeiro que falhar."""
        if stage != self.stage:
            return InspectionVerdict(False, "INSPECTION_STAGE_MISMATCH")
        if method.upper() != self.method.upper():
            return InspectionVerdict(False, "INSPECTION_METHOD_MISMATCH")
        if _origin_of(url) != self.origin.rstrip("/"):
            return InspectionVerdict(False, "INSPECTION_ORIGIN_MISMATCH")
        parsed = urlsplit(url)
        if re.fullmatch(self.path_pattern, parsed.path) is None:
            return InspectionVerdict(False, "INSPECTION_PATH_MISMATCH")
        payload = _json_object(body)
        if payload is None:
            return InspectionVerdict(False, "INSPECTION_BODY_UNPARSEABLE")
        operation = str(payload.get("operationName") or "")
        if operation not in self.operation_names:
            return InspectionVerdict(False, "INSPECTION_OPERATION_NOT_ALLOWED")
        # O board repete a operação na query string (`?op=...`). Se as duas
        # divergirem, algo foi montado à mão: recusa.
        hint = parse_qs(parsed.query).get("op", [""])[0]
        if hint and hint != operation:
            return InspectionVerdict(False, "INSPECTION_OPERATION_HINT_MISMATCH", operation)
        if not graphql_query_is_read_only(str(payload.get("query") or "")):
            return InspectionVerdict(False, "INSPECTION_NOT_A_QUERY", operation)
        variables = payload.get("variables")
        if variables is None:
            variables = {}
        if not isinstance(variables, dict):
            return InspectionVerdict(False, "INSPECTION_BODY_UNPARSEABLE", operation)
        expected = {name: value for name, value in self.expected_variables}
        optional = {name: value for name, value in self.optional_variables}
        present = {str(key) for key in variables}
        # Vinculo obrigatorio: a vaga e o board precisam ser exatamente estes.
        if not set(expected).issubset(present):
            return InspectionVerdict(False, "INSPECTION_RESOURCE_MISMATCH", operation)
        # Nenhuma variavel fora do que a operacao declara: uma variavel a mais
        # poderia mudar o recurso consultado.
        if not present.issubset(set(expected) | set(optional)):
            return InspectionVerdict(False, "INSPECTION_RESOURCE_MISMATCH", operation)
        for name, value in {**expected, **optional}.items():
            if name in variables and str(variables.get(name)) != value:
                return InspectionVerdict(False, "INSPECTION_RESOURCE_MISMATCH", operation)
        return InspectionVerdict(True, "INSPECTION_OK", operation)


@dataclass(frozen=True)
class InspectionNetworkPolicy:
    """Política de rede da fase de descoberta, derivada do provider.

    Carrega as permissões já vinculadas a UMA vaga: o board e o id vêm da vaga
    corrente, então um permit jamais serve para consultar outra vaga ou outra
    organização. Só vale para ``FORM_DISCOVERY`` e não autoriza upload nem
    submissão — esses continuam com suas próprias boundaries.
    """

    provider: str
    application_id: str
    stage: str
    origin: str
    method: str
    authorized: tuple[AuthorizedInspectionRequest, ...]

    @classmethod
    def for_form_discovery(
        cls,
        provider: str,
        application_id: str,
        *,
        board: str,
        external_id: str,
    ) -> "InspectionNetworkPolicy":
        try:
            profile = profile_for(provider)
        except ProviderError as exc:
            raise InspectionBoundaryError(f"no inspection policy for provider: {provider}") from exc
        if not profile.inspection_operations:
            raise InspectionBoundaryError(f"provider has no read-only inspection operations: {provider}")
        if not profile.inspection_origin or not profile.inspection_path:
            raise InspectionBoundaryError(f"provider has no inspection endpoint: {provider}")
        if not board or not external_id:
            # Sem vínculo não há como prender a requisição à vaga: recusar é
            # melhor do que emitir um permit que valeria para qualquer recurso.
            raise InspectionBoundaryError("inspection policy requires board and external_id bindings")
        context = {"board": board, "external_id": external_id}
        authorized: list[AuthorizedInspectionRequest] = []
        for operation in profile.inspection_operations:
            try:
                required, optional = operation.resolved_variables(context)
            except ProviderError as exc:
                raise InspectionBoundaryError(str(exc)) from exc
            authorized.append(
                AuthorizedInspectionRequest(
                    application_id=application_id,
                    provider=provider,
                    origin=profile.inspection_origin,
                    path_pattern=r"^" + re.escape(operation.path) + r"$",
                    method=profile.inspection_method,
                    operation_names=(operation.name,),
                    expected_variables=tuple(sorted(required.items())),
                    optional_variables=tuple(sorted(optional.items())),
                    max_requests=profile.inspection_max_requests,
                )
            )
        return cls(
            provider=provider,
            application_id=application_id,
            stage=INSPECTION_STAGE_FORM_DISCOVERY,
            origin=profile.inspection_origin,
            method=profile.inspection_method,
            authorized=tuple(authorized),
        )

    def permits(self) -> list[AuthorizedInspectionRequest]:
        return list(self.authorized)
