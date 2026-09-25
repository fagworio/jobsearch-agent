"""RA-SI-001: o supervisor classifica e PARA — nunca transforma loop de dev em loop de POST."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import sys

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ra_si", REPO / "scripts" / "real_apply_self_improve.py")
assert spec is not None and spec.loader is not None
ra_si = importlib.util.module_from_spec(spec)
# O modulo PRECISA estar em sys.modules ANTES do exec: no 3.14 o `@dataclass` faz
# `sys.modules[cls.__module__].__dict__`, e sem o registro o import morre com
# `'NoneType' object has no attribute '__dict__'`.
sys.modules["ra_si"] = ra_si
spec.loader.exec_module(ra_si)


def _durable(state, attempts=(), intents=()):
    return ra_si.Durable("app-1", state, tuple(attempts), tuple(intents))


def _attempt(status="SUBMITTING", write_possible_at=""):
    return ra_si.AttemptView("attempt-1", status, write_possible_at)


def _run(status="", writes=None, exit_code=2, timed_out=False):
    payload = {"status": status}
    if writes is not None:
        payload["submission_writes"] = writes
    return ra_si.RunResult(exit_code=exit_code, payload=payload, timed_out=timed_out)


def test_submitted_is_success():
    assert ra_si.classify(_run("SUBMITTED"), _durable("SUBMITTED"))[0] == ra_si.SUCCESS


def test_post_write_states_stop():
    assert ra_si.classify(_run(""), _durable("SUBMIT_UNKNOWN"))[0] == ra_si.POST_WRITE_STOP
    assert ra_si.classify(_run(""), _durable("AWAITING_SUBMISSION_CONFIRMATION"))[0] == ra_si.POST_WRITE_STOP
    assert ra_si.classify(_run(""), _durable("HANDOFF_IN_PROGRESS"))[0] == ra_si.POST_WRITE_STOP


def test_human_states_stop():
    for state in ("NEEDS_ANSWER", "NEEDS_ARTIFACT", "NEEDS_LOGIN", "NEEDS_MFA", "NEEDS_CAPTCHA", "NEEDS_HUMAN_CAPTCHA", "POLICY_BLOCKED", "REJECTED"):
        assert ra_si.classify(_run(""), _durable(state))[0] == ra_si.HUMAN_REQUIRED, state


def test_submitting_with_crossed_boundary_is_hard_stop():
    crossed = (_attempt(write_possible_at="2026-09-25T10:00:00+00:00"),)
    assert ra_si.classify(_run(""), _durable("SUBMITTING", crossed))[0] == ra_si.POST_WRITE_STOP


def test_submitting_without_boundary_is_a_prewrite_blocker():
    empty = (_attempt(write_possible_at=""),)
    assert ra_si.classify(_run(""), _durable("SUBMITTING", empty))[0] == ra_si.PREWRITE_BLOCKER


def test_submit_failed_needs_proof_of_zero_writes():
    assert ra_si.classify(_run("SUBMIT_FAILED", writes=0), _durable("REVIEW_REACHED"))[0] == ra_si.PREWRITE_BLOCKER
    assert ra_si.classify(_run("SUBMIT_FAILED", writes=1), _durable("SUBMIT_FAILED"))[0] == ra_si.POST_WRITE_STOP
    assert ra_si.classify(_run("SUBMIT_FAILED", writes=None), _durable("SUBMIT_FAILED"))[0] == ra_si.POST_WRITE_STOP
    crossed = (_attempt(write_possible_at="2026-09-25T10:00:00+00:00"),)
    assert ra_si.classify(_run("SUBMIT_FAILED", writes=0), _durable("SUBMIT_FAILED", crossed))[0] == ra_si.POST_WRITE_STOP


def test_timeout_without_database_proof_stops():
    """Timeout = sem prova de zero writes e sem tentativa identificavel: PARA."""
    assert ra_si.classify(_run(timed_out=True), _durable("REVIEW_REACHED"))[0] == ra_si.POST_WRITE_STOP


def test_timeout_in_submitting_is_decided_by_the_recovery_not_by_the_clock():
    """`SUBMITTING` nao se classifica pelo timeout: o RECOVERY le a fronteira.

    Este teste fixa a ORDEM do contrato: para `SUBMITTING`, quem decide e
    `recover_stranded_submission` (uma vez, depois que o filho terminou) — marcador
    vazio significa pre-write e o ciclo pode continuar; `unknown` e HARD STOP.
    """
    empty = (_attempt(write_possible_at=""),)
    assert ra_si.classify(_run(timed_out=True), _durable("SUBMITTING", empty))[0] == ra_si.PREWRITE_BLOCKER
    crossed = (_attempt(write_possible_at="2026-09-25T10:00:00+00:00"),)
    assert ra_si.classify(_run(timed_out=True), _durable("SUBMITTING", crossed))[0] == ra_si.POST_WRITE_STOP


def test_a_failure_before_any_attempt_is_a_prewrite_blocker():
    assert ra_si.classify(_run("ERROR"), _durable("READY_TO_APPLY"))[0] == ra_si.PREWRITE_BLOCKER


def test_the_fixer_may_not_run_the_real_application():
    with pytest.raises(ra_si.SupervisorError):
        ra_si.assert_fixer_cannot_submit("codex exec --file {prompt_file} && apply-to-completion x --submit")


def test_forbidden_paths_never_enter_a_commit():
    assert ra_si.forbidden_in_diff(["src/jobsearch_agent/loop.py"]) == []
    local_profile = "profile/" + "career_profile" + ".local.yaml"
    for bad in (local_profile, "data/jobsearch.db", "data/applications/resume.pdf"):
        assert ra_si.forbidden_in_diff([bad]) == [bad]


def test_the_context_carries_no_private_data():
    context = ra_si.build_context(
        iteration=1, sha="abc", branch="real-apply-001", job_id="job", application_id="app",
        result=_run("ERROR"), durable=_durable("READY_TO_APPLY"), reason="r",
    )
    rendered = repr(context).casefold()
    for forbidden in ("@", "curriculo", "resume.pdf", "post_data", "password", "cookie"):
        assert forbidden not in rendered, forbidden
    secret_result = _run("ERROR")
    secret_result = ra_si.RunResult(secret_result.exit_code, secret_result.payload, "password=secret-token")
    safe_context = ra_si.build_context(
        iteration=1, sha="abc", branch="real-apply-001", job_id="job", application_id="app",
        result=secret_result, durable=_durable("READY_TO_APPLY"), reason="r",
    )
    assert safe_context["stdout_tail"] == ""
    assert "secret-token" not in repr(safe_context)


def test_fixer_command_is_allowlisted():
    ra_si.assert_fixer_cannot_submit("codex exec --file {prompt_file}")
    for command in (
        "python -c 'print(1)'",
        "codex exec --file {prompt_file} --submit",
        "codex exec --file {prompt_file} && git push",
    ):
        with pytest.raises(ra_si.SupervisorError):
            ra_si.assert_fixer_cannot_submit(command)


def test_recovery_prewrite_is_classified_before_fixer(tmp_path, monkeypatch):
    args = ra_si.parse_args(["--job-id", "j", "--application-id", "a", "--journal-dir", str(tmp_path)])
    states = iter((_durable("SUBMITTING", (_attempt(),)), _durable("REVIEW_REACHED")))
    monkeypatch.setattr(ra_si, "git", lambda *a: ra_si.subprocess.CompletedProcess(a, 0, "abc123\n", ""))
    monkeypatch.setattr(ra_si, "inspect_database", lambda database, application_id: next(states))
    monkeypatch.setattr(ra_si, "recover_if_stranded", lambda database, durable: ("REVIEW_REACHED", "pre_write"))

    exit_code = ra_si.supervisor_loop(
        database=object(), args=args, run_application=lambda: _run("ERROR"), fix=None
    )

    assert exit_code == 6
    journal = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "recovery confirmou ausencia de escrita" in journal


def test_the_loop_stops_on_hard_stop_without_calling_the_fixer(tmp_path, monkeypatch):
    args = ra_si.parse_args(["--job-id", "j", "--application-id", "a", "--journal-dir", str(tmp_path)])
    calls = {"fix": 0}

    def fake_fix(prompt, timeout):
        calls["fix"] += 1
        return 0

    monkeypatch.setattr(ra_si, "git", lambda *a: ra_si.subprocess.CompletedProcess(a, 0, "abc123\n", ""))
    monkeypatch.setattr(ra_si, "inspect_database", lambda database, application_id: _durable("SUBMIT_UNKNOWN"))

    exit_code = ra_si.supervisor_loop(
        database=object(), args=args, run_application=lambda: _run("SUBMIT_UNKNOWN"), fix=fake_fix
    )

    assert exit_code == 3
    assert calls["fix"] == 0
    assert (tmp_path / "journal.jsonl").exists()
