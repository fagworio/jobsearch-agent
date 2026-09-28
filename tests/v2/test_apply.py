"""O incremento termina em NEEDS_INPUT e nao escreve nada."""

from pathlib import Path

from job_agent_v2.apply import apply
from job_agent_v2.models import State

FIXTURES = Path(__file__).parent / "fixtures"
PROMPT = "Have you ever been previously employed by Spotify?"


def _load(_url: str) -> str:
    return (FIXTURES / "card_checkbox_spotify.html").read_text(encoding="utf-8")


def test_a_missing_fact_ends_in_needs_input():
    # o email vem do profile para que a UNICA pendencia seja o fato da pessoa
    result = apply("https://jobs.lever.co/spotify/x", profile={"email": "a@b.test"}, open_page=_load)
    assert result.state is State.NEEDS_INPUT
    assert result.reason == "missing_answer"
    assert [field.prompt for field in result.missing] == [PROMPT]


def test_needs_input_reports_zero_writes():
    payload = apply("https://jobs.lever.co/spotify/x", open_page=_load).to_dict()
    assert payload["uploads"] == 0
    assert payload["attempts"] == 0
    assert payload["submission_writes"] == 0


def test_an_approved_answer_makes_it_ready():
    result = apply("https://jobs.lever.co/spotify/x", approved={PROMPT: "No"}, profile={"email": "a@b.test"}, open_page=_load)
    assert result.state is State.READY
    assert result.missing == ()
    assert len(result.answers) == 2


def test_a_trivial_profile_field_is_used_only_for_trivial_kinds():
    result = apply("https://jobs.lever.co/spotify/x", profile={"email": "a@b.test"}, open_page=_load)
    assert result.state is State.NEEDS_INPUT
    assert PROMPT in [field.prompt for field in result.missing]


def test_no_v1_import_is_needed_to_run_the_slice():
    # O teste precisa ser independente da ordem de coleta: a suíte legacy pode
    # já ter importado jobsearch_agent no mesmo processo do pytest.
    import os
    import subprocess
    import sys

    source_root = str(Path(__file__).resolve().parents[2] / "src")
    env = dict(os.environ, PYTHONPATH=source_root)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import job_agent_v2.apply, job_agent_v2.fill, job_agent_v2.submit; "
                "assert not any(name.startswith(('jobsearch_agent', 'challenge_resolution', 'challenge_guard')) "
                "for name in sys.modules)"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr


# Regressao do blocker real: a pagina da VAGA nao e o formulario (0 inputs) e o
# link `apply for this job` aponta para a rota que tem o formulario de verdade.
JOB_PAGE = (
    '<html><body><a data-qa="show-page-apply" '
    'href="https://jobs.lever.co/spotify/x/apply">apply for this job</a></body></html>'
)


def test_the_apply_link_of_the_job_page_is_read_and_followed():
    seen: list[str] = []

    def load(url: str) -> str:
        seen.append(url)
        if url.endswith("/apply"):
            return (FIXTURES / "card_checkbox_spotify.html").read_text(encoding="utf-8")
        return JOB_PAGE

    result = apply("https://jobs.lever.co/spotify/x", profile={"email": "a@b.test"}, open_page=load)
    assert seen == ["https://jobs.lever.co/spotify/x", "https://jobs.lever.co/spotify/x/apply"]
    assert result.apply_url == "https://jobs.lever.co/spotify/x/apply"
    assert result.fields > 0
    assert [field.prompt for field in result.missing] == [PROMPT]


def test_zero_fields_never_becomes_ready():
    result = apply("https://jobs.lever.co/spotify/x", open_page=lambda _url: "<html><body>no form</body></html>")
    assert result.state is State.NEEDS_INPUT
    assert result.reason == "form_not_found"
    assert result.fields == 0
