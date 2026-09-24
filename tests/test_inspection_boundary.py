"""Boundary de inspeção read-only (POST que não é escrita).

O Ashby monta o formulário a partir de `POST /api/non-user-graphql`. Validar só
URL e método não basta: no mesmo endpoint cabem várias operações, e uma delas
pode ser `mutation`. Estes testes fixam cada portão da decisão.
"""

from __future__ import annotations

import json

import pytest

from jobsearch_agent.browser import NetworkWriteGuard, NetworkRequestEvent  # noqa: F401
from jobsearch_agent.inspection import (
    AuthorizedInspectionRequest,
    InspectionBoundaryError,
    InspectionNetworkPolicy,
    graphql_query_is_read_only,
)
from jobsearch_agent.providers import board_from_url


BOARD = "linear"
JOB_ID = "d3bc1ced-3ce4-4086-a050-555055dbb1ff"
ENDPOINT = f"https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"

QUERY_JOB = (
    "query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) { "
    "jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, "
    "jobPostingId: $jobPostingId) { id title } }"
)


def _body(operation="ApiJobPosting", query=QUERY_JOB, variables=None, drop_variables=False):
    payload = {"operationName": operation, "query": query}
    if not drop_variables:
        payload["variables"] = (
            {"organizationHostedJobsPageName": BOARD, "jobPostingId": JOB_ID}
            if variables is None
            else variables
        )
    return json.dumps(payload)


def _policy():
    return InspectionNetworkPolicy.for_form_discovery("ashby", "app-1", board=BOARD, external_id=JOB_ID)


def _permit(operation="ApiJobPosting", variables=None):
    return AuthorizedInspectionRequest(
        application_id="app-1",
        provider="ashby",
        origin="https://jobs.ashbyhq.com",
        path_pattern=r"^/api/non-user-graphql$",
        method="POST",
        operation_names=(operation,),
        expected_variables=tuple(
            sorted((variables or {"organizationHostedJobsPageName": BOARD, "jobPostingId": JOB_ID}).items())
        ),
    )


def _verdict(permit, *, method="POST", url=ENDPOINT, body=None, stage="FORM_DISCOVERY"):
    return permit.validate(method=method, url=url, body=_body() if body is None else body, stage=stage)


# --- o caminho feliz ---------------------------------------------------------


def test_the_real_ashby_request_is_covered():
    verdict = _verdict(_permit())
    assert verdict.covered is True
    assert verdict.reason_token == "INSPECTION_OK"
    assert verdict.operation_name == "ApiJobPosting"


def test_policy_binds_both_operations_to_the_current_job():
    permits = _policy().permits()
    assert [name for permit in permits for name in permit.operation_names] == [
        "ApiJobPosting",
        "ApiOrganizationFromHostedJobsPageName",
    ]
    job_permit = permits[0]
    assert dict(job_permit.expected_variables) == {
        "organizationHostedJobsPageName": BOARD,
        "jobPostingId": JOB_ID,
    }
    # A segunda operacao e vinculada ao board; `searchContext` e opcional porque
    # o proprio SPA chama a operacao com e sem ela.
    assert dict(permits[1].expected_variables) == {"organizationHostedJobsPageName": BOARD}
    assert dict(permits[1].optional_variables) == {"searchContext": "JobPosting"}


def test_an_optional_variable_may_be_absent_but_not_wrong():
    """O SPA chama a mesma operacao com e sem `searchContext`."""
    permit = AuthorizedInspectionRequest(
        application_id="app-1",
        provider="ashby",
        origin="https://jobs.ashbyhq.com",
        path_pattern=r"^/api/non-user-graphql$",
        method="POST",
        operation_names=("ApiOrganizationFromHostedJobsPageName",),
        expected_variables=(("organizationHostedJobsPageName", BOARD),),
        optional_variables=(("searchContext", "JobPosting"),),
    )
    url = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiOrganizationFromHostedJobsPageName"
    query = "query ApiOrganizationFromHostedJobsPageName($organizationHostedJobsPageName: String!) { organization { id } }"

    without = _body(operation="ApiOrganizationFromHostedJobsPageName", query=query,
                    variables={"organizationHostedJobsPageName": BOARD})
    assert _verdict(permit, url=url, body=without).covered is True

    with_it = _body(operation="ApiOrganizationFromHostedJobsPageName", query=query,
                    variables={"organizationHostedJobsPageName": BOARD, "searchContext": "JobPosting"})
    assert _verdict(permit, url=url, body=with_it).covered is True

    # Presente com valor diferente continua recusado, e o vinculo obrigatorio nunca afrouxa.
    wrong = _body(operation="ApiOrganizationFromHostedJobsPageName", query=query,
                  variables={"organizationHostedJobsPageName": BOARD, "searchContext": "Organization"})
    assert _verdict(permit, url=url, body=wrong).reason_token == "INSPECTION_RESOURCE_MISMATCH"

    missing_required = _body(operation="ApiOrganizationFromHostedJobsPageName", query=query,
                             variables={"searchContext": "JobPosting"})
    assert _verdict(permit, url=url, body=missing_required).reason_token == "INSPECTION_RESOURCE_MISMATCH"


# --- cada portao de recusa ---------------------------------------------------


def test_mutation_is_never_covered():
    mutation = _body(query="mutation ApiJobPosting { deleteJobPosting(id: \"x\") { id } }")
    verdict = _verdict(_permit(), body=mutation)
    assert verdict.covered is False
    assert verdict.reason_token == "INSPECTION_NOT_A_QUERY"


def test_subscription_is_never_covered():
    body = _body(query="subscription ApiJobPosting { jobPosting { id } }")
    assert _verdict(_permit(), body=body).reason_token == "INSPECTION_NOT_A_QUERY"


def test_mentioning_mutation_in_a_comment_or_string_stays_a_query():
    """Mencionar nao e executar: comentario e literal nao mudam a operacao."""
    commented = _body(query="query ApiJobPosting { jobPosting { id } } # mutation")
    assert _verdict(_permit(), body=commented).covered is True
    assert graphql_query_is_read_only("query X { a } # mutation") is True
    assert graphql_query_is_read_only('query X { a }') is True


def test_an_unterminated_literal_is_refused_instead_of_swallowing_the_rest():
    """Bypass fechado: um literal aberto engoliria o `mutation` que vem depois.

    O servidor recusaria o documento malformado, mas a decisao aqui nao pode
    depender disso: quando nao da para ler o resto, recusa.
    """
    assert graphql_query_is_read_only('query X { a } "  mutation { x }') is False
    assert graphql_query_is_read_only('query X { a } """  mutation { x }') is False
    opaque = _body(query='query X { a } "  mutation ApiJobPosting { x { id } }')
    assert _verdict(_permit(), body=opaque).reason_token == "INSPECTION_NOT_A_QUERY"


def test_a_document_without_an_operation_keyword_is_refused():
    assert graphql_query_is_read_only("jobPosting { id }") is False
    assert graphql_query_is_read_only("") is False
    assert graphql_query_is_read_only("   ") is False
    # Selecao anonima e uma query valida em GraphQL.
    assert graphql_query_is_read_only("{ jobPosting { id } }") is True


def test_operation_outside_the_allowlist_is_refused():
    body = _body(operation="ApiDeleteJobPosting", query="query ApiDeleteJobPosting { jobPosting { id } }")
    verdict = _verdict(_permit(), body=body)
    assert verdict.covered is False
    assert verdict.reason_token == "INSPECTION_OPERATION_NOT_ALLOWED"


def test_operation_hint_disagreeing_with_the_body_is_refused():
    """O board repete a operacao em ?op=; divergencia significa pedido montado a mao."""
    body = _body(operation="ApiJobPosting")
    url = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiOrganizationFromHostedJobsPageName"
    verdict = _verdict(_permit(), url=url, body=body)
    assert verdict.covered is False
    assert verdict.reason_token == "INSPECTION_OPERATION_HINT_MISMATCH"


def test_another_job_or_board_is_refused():
    other_job = _body(variables={"organizationHostedJobsPageName": BOARD, "jobPostingId": "outra-vaga"})
    assert _verdict(_permit(), body=other_job).reason_token == "INSPECTION_RESOURCE_MISMATCH"
    other_board = _body(variables={"organizationHostedJobsPageName": "outra-org", "jobPostingId": JOB_ID})
    assert _verdict(_permit(), body=other_board).reason_token == "INSPECTION_RESOURCE_MISMATCH"


def test_extra_variables_are_refused():
    """O conjunto de variaveis e exato: variavel a mais muda a operacao efetiva."""
    body = _body(
        variables={
            "organizationHostedJobsPageName": BOARD,
            "jobPostingId": JOB_ID,
            "extra": "1",
        }
    )
    assert _verdict(_permit(), body=body).reason_token == "INSPECTION_RESOURCE_MISMATCH"


def test_missing_variables_are_refused():
    assert _verdict(_permit(), body=_body(drop_variables=True)).reason_token == "INSPECTION_RESOURCE_MISMATCH"


def test_wrong_origin_path_and_method_are_refused():
    assert _verdict(_permit(), url="https://evil.example/api/non-user-graphql?op=ApiJobPosting").reason_token == (
        "INSPECTION_ORIGIN_MISMATCH"
    )
    assert _verdict(_permit(), url="https://jobs.ashbyhq.com/api/outro?op=ApiJobPosting").reason_token == (
        "INSPECTION_PATH_MISMATCH"
    )
    assert _verdict(_permit(), method="GET").reason_token == "INSPECTION_METHOD_MISMATCH"


def test_wrong_stage_is_refused():
    """A permissao vale para a descoberta do formulario, nao para a submissao."""
    assert _verdict(_permit(), stage="SUBMIT").reason_token == "INSPECTION_STAGE_MISMATCH"


def test_unparseable_body_is_refused():
    assert _verdict(_permit(), body="nao e json").reason_token == "INSPECTION_BODY_UNPARSEABLE"
    assert _verdict(_permit(), body="[]").reason_token == "INSPECTION_BODY_UNPARSEABLE"


# --- orcamento e separacao de contratos --------------------------------------


def test_the_guard_spends_its_own_budget_and_never_a_write_budget():
    guard = NetworkWriteGuard({"jobs.ashbyhq.com"})
    guard.arm_inspections([_permit()])
    request = _FakeRequest("POST", ENDPOINT, _body())
    assert guard.inspect(request) is True
    assert guard.inspections_used == 1
    # Nenhum credito de escrita foi tocado, e a escrita continua bloqueada.
    assert guard.authorized_writes_used == 0
    assert guard.inspect(_FakeRequest("POST", "https://jobs.ashbyhq.com/apply", "{}")) is False


def test_the_budget_is_finite_and_exhaustion_is_audited():
    guard = NetworkWriteGuard({"jobs.ashbyhq.com"})
    guard.arm_inspections([_permit()])
    assert guard.inspect(_FakeRequest("POST", ENDPOINT, _body())) is True
    assert guard.inspections_used == 1
    # Orcamento esgotado: a MESMA requisicao deixa de ser coberta.
    assert guard.inspect(_FakeRequest("POST", ENDPOINT, _body())) is False
    assert guard.inspections_used == 1
    assert guard.events[-1].reason == "INSPECTION_BUDGET_EXHAUSTED"


def test_two_permits_do_not_share_a_budget():
    """Cada operacao tem o proprio credito: gastar um nao consome o outro."""
    guard = NetworkWriteGuard({"jobs.ashbyhq.com"})
    guard.arm_inspections(_policy().permits())
    org_url = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiOrganizationFromHostedJobsPageName"
    org_body = _body(
        operation="ApiOrganizationFromHostedJobsPageName",
        query="query ApiOrganizationFromHostedJobsPageName($organizationHostedJobsPageName: String!) { organization { id } }",
        variables={"organizationHostedJobsPageName": BOARD, "searchContext": "JobPosting"},
    )
    assert guard.inspect(_FakeRequest("POST", ENDPOINT, _body())) is True
    assert guard.inspect(_FakeRequest("POST", org_url, org_body)) is True
    assert guard.inspections_used == 2
    assert [used for _permit, used in guard.inspection_usage] == [1, 1]


def test_a_mutation_is_blocked_even_with_a_permit_armed():
    guard = NetworkWriteGuard({"jobs.ashbyhq.com"})
    guard.arm_inspections([_permit()])
    mutation = _body(query="mutation ApiJobPosting { x { id } }")
    assert guard.inspect(_FakeRequest("POST", ENDPOINT, mutation)) is False
    assert guard.inspections_used == 0


def test_no_inspection_permit_keeps_the_old_dry_run_behaviour():
    guard = NetworkWriteGuard({"jobs.ashbyhq.com"})
    assert guard.inspect(_FakeRequest("POST", ENDPOINT, _body())) is False
    blocked = guard.blocked_writes
    assert len(blocked) == 1
    assert "dry-run" in blocked[0].reason


def test_policy_refuses_without_bindings_or_operations():
    with pytest.raises(InspectionBoundaryError):
        InspectionNetworkPolicy.for_form_discovery("ashby", "app-1", board="", external_id=JOB_ID)
    with pytest.raises(InspectionBoundaryError):
        InspectionNetworkPolicy.for_form_discovery("ashby", "app-1", board=BOARD, external_id="")
    # Lever nao declara inspecao read-only: nao ha o que autorizar.
    with pytest.raises(InspectionBoundaryError):
        InspectionNetworkPolicy.for_form_discovery("lever", "app-1", board="ciandt", external_id="x")
    with pytest.raises(InspectionBoundaryError):
        InspectionNetworkPolicy.for_form_discovery("desconhecido", "app-1", board="b", external_id="e")


def test_board_from_url_derives_the_slug():
    assert board_from_url("ashby", f"https://jobs.ashbyhq.com/{BOARD}/{JOB_ID}") == BOARD
    assert board_from_url("lever", "https://jobs.lever.co/ciandt/59494544") == "ciandt"
    assert board_from_url("greenhouse", "https://boards.greenhouse.io/canonical/jobs/5150422") == "canonical"


class _FakeRequest:
    def __init__(self, method: str, url: str, body: str):
        self.method = method
        self.url = url
        self.resource_type = "xhr"
        self.post_data = body
