"""RA-SI-001: supervisor de self-improvement do REAL-APPLY-001.

Objetivo unico: levar UMA candidatura real ate `SUBMITTED`, corrigindo apenas
blockers PRE-WRITE comprovadamente seguros de repetir.

```text
run real  ->  le stdout + banco  ->  classifica
   SUCCESS           -> exit 0
   PREWRITE_BLOCKER  -> contexto -> fixer -> valida -> commit -> run de novo
   HUMAN_REQUIRED    -> HARD STOP
   POST_WRITE_STOP   -> HARD STOP
```

Duas autoridades que NAO se misturam:

* **somente o supervisor** executa `apply-to-completion --submit`;
* o **fixer** (agente de codigo externo) so recebe um `prompt.md` e edita codigo.

O supervisor nunca decide por heuristica que "pode reenviar": se nao houver prova
duravei de zero escritas, ele PARA. `write_possible_at` preenchido sem essa prova
e HARD STOP — o marcador e conservador de proposito.

Uso:
    python scripts/real_apply_self_improve.py --job-id JOB --application-id APP \
        --fix-command 'codex exec --file {prompt_file}'
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any, Callable, Sequence

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from jobsearch_agent.models import ApplicationState  # noqa: E402
from jobsearch_agent.persistence import ApplicationConflict, Database  # noqa: E402

#: Desfechos.
SUCCESS = "SUCCESS"
PREWRITE_BLOCKER = "PREWRITE_BLOCKER"
HUMAN_REQUIRED = "HUMAN_REQUIRED"
POST_WRITE_STOP = "POST_WRITE_STOP"

#: Estados que exigem uma pessoa (o fixer nunca inventa dado pessoal/legal).
HUMAN_STATES = frozenset(
    {
        ApplicationState.NEEDS_ANSWER.value,
        ApplicationState.NEEDS_ARTIFACT.value,
        ApplicationState.NEEDS_LOGIN.value,
        ApplicationState.NEEDS_MFA.value,
        ApplicationState.NEEDS_CAPTCHA.value,
        ApplicationState.NEEDS_HUMAN_CAPTCHA.value,
        ApplicationState.POLICY_BLOCKED.value,
        ApplicationState.REJECTED.value,
    }
)

#: Estados pos-write: nunca reexecutar automaticamente.
POST_WRITE_STATES = frozenset(
    {
        ApplicationState.SUBMIT_UNKNOWN.value,
        ApplicationState.AWAITING_SUBMISSION_CONFIRMATION.value,
        ApplicationState.HANDOFF_IN_PROGRESS.value,
    }
)

#: Caminhos que NUNCA entram num commit do supervisor.
FORBIDDEN_COMMIT_PATTERNS = (
    "profile/",
    "data/",
    ".db",
    ".sqlite",
    "resume",
    "curriculo",
    ".local.yaml",
    "applications/",
)
ALLOWED_FIX_PATH_PREFIXES = ("src/", "tests/")
ALLOWED_FIX_COMMAND = ("codex", "exec", "--file", "{prompt_file}")


class SupervisorError(RuntimeError):
    """Falha de operacao do supervisor (nunca de classificacao)."""


# -- observacao do mundo --------------------------------------------------------


@dataclass(frozen=True)
class AttemptView:
    id: str
    status: str
    write_possible_at: str

    @property
    def boundary_crossed(self) -> bool:
        return bool(self.write_possible_at)


@dataclass(frozen=True)
class Durable:
    """O que o banco diz. Fonte de verdade depois da execucao."""

    application_id: str
    state: str
    attempts: tuple[AttemptView, ...] = ()
    intents: tuple[tuple[str, str], ...] = ()

    @property
    def last_attempt(self) -> AttemptView | None:
        return self.attempts[-1] if self.attempts else None


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    payload: dict[str, Any] = field(default_factory=dict)
    stdout_tail: str = ""
    timed_out: bool = False

    @property
    def status(self) -> str:
        return str(self.payload.get("status", "") or "")

    @property
    def submission_writes(self) -> int | None:
        value = self.payload.get("submission_writes")
        return int(value) if isinstance(value, int) else None

    @property
    def upload_writes_used(self) -> int | None:
        value = self.payload.get("upload_writes_used")
        return int(value) if isinstance(value, int) else None

    @property
    def phases(self) -> tuple[str, ...]:
        return tuple(str(item) for item in self.payload.get("phases", ()) or ())


def inspect_database(database: Database, application_id: str) -> Durable:
    application = database.get_application(application_id)
    if application is None:
        raise SupervisorError(f"application not found: {application_id}")
    attempts = tuple(
        AttemptView(item.id, item.status, getattr(item, "write_possible_at", "") or "")
        for item in database.list_submission_attempts(application_id)
    )
    intents = tuple((item.id, item.status) for item in database.list_submission_intents(application_id))
    return Durable(application_id, application.state.value, attempts, intents)


# -- classificacao --------------------------------------------------------------


def classify(result: RunResult, durable: Durable) -> tuple[str, str]:
    """(desfecho, motivo). Nunca inventa permissao de reenvio."""
    if durable.state == ApplicationState.SUBMITTED.value:
        return SUCCESS, "application persistida em SUBMITTED"
    if durable.last_attempt is not None and durable.last_attempt.boundary_crossed:
        return POST_WRITE_STOP, "tentativa persistida com write_possible_at preenchido"
    if durable.state in HUMAN_STATES:
        return HUMAN_REQUIRED, f"estado exige pessoa: {durable.state}"
    if durable.state == ApplicationState.SUBMITTING.value:
        # Fronteira cruzada ou tentativa ambigua: quem decide e o recovery, e ele
        # roda UMA vez depois que o processo filho terminou (nunca durante).
        last = durable.last_attempt
        if last is not None and last.boundary_crossed:
            return POST_WRITE_STOP, "SUBMITTING com write_possible_at preenchido"
        return PREWRITE_BLOCKER, "SUBMITTING sem fronteira cruzada (recovery decide)"
    if durable.state in POST_WRITE_STATES:
        return POST_WRITE_STOP, f"estado pos-write: {durable.state}"

    if result.timed_out:
        return POST_WRITE_STOP, "processo excedeu o timeout (desfecho desconhecido)"

    if result.status == ApplicationState.SUBMIT_FAILED.value:
        if result.submission_writes == 0:
            return PREWRITE_BLOCKER, "SUBMIT_FAILED com prova de zero escritas"
        return POST_WRITE_STOP, "SUBMIT_FAILED sem prova de zero escritas"

    if result.status == ApplicationState.SUBMIT_UNKNOWN.value:
        return POST_WRITE_STOP, "SUBMIT_UNKNOWN"

    if durable.state == ApplicationState.SUBMIT_FAILED.value:
        if result.submission_writes == 0:
            return PREWRITE_BLOCKER, "SUBMIT_FAILED (banco) com prova de zero escritas"
        return POST_WRITE_STOP, "SUBMIT_FAILED (banco) sem prova de zero escritas"

    if not durable.attempts:
        return PREWRITE_BLOCKER, f"falha antes de begin_submission (status={result.status or 'sem resultado'})"

    return POST_WRITE_STOP, f"desfecho nao classificado como pre-write (status={result.status})"


# -- contexto e fixer -----------------------------------------------------------


def build_context(
    *,
    iteration: int,
    sha: str,
    branch: str,
    job_id: str,
    application_id: str,
    result: RunResult,
    durable: Durable,
    reason: str,
    changed_files: Sequence[str] = (),
) -> dict[str, Any]:
    """Contexto MINIMO para o fixer. Sem PII, sem corpo, sem resposta de formulario."""
    return {
        "iteration": iteration,
        "sha": sha,
        "branch": branch,
        "job_id": job_id,
        "application_id": application_id,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "status": result.status,
        "state": durable.state,
        "reason": reason,
        "phases": list(result.phases),
        "attempts": [
            {"id": a.id, "status": a.status, "write_possible_at": a.write_possible_at} for a in durable.attempts
        ],
        "intents": [{"id": i, "status": s} for i, s in durable.intents],
        "writes": {
            "submission_writes": result.submission_writes,
            "upload_writes_used": result.upload_writes_used,
        },
        "stdout_tail": "",
        "changed_files": list(changed_files),
    }


PROMPT_TEMPLATE = """# Corrigir blocker do REAL-APPLY-001 (iteration {iteration})

Contexto: `{context_file}` (leia; nao ha PII ali).

Blockers atuais:

    state   = {state}
    status  = {status}
    reason  = {reason}
    sha     = {sha}

Ordens, em ordem de prioridade:

1. LEIA o codigo antes de editar (o `reason` acima indica o caminho).
2. Localize a causa CONCRETA deste blocker; nao corrija o que nao foi observado.
3. Reproduza o defeito em teste DETERMINISTICO antes da correcao, quando possivel.
4. Aplique o MENOR patch que permita a MESMA vaga (`{job_id}`) avancar.
5. Nao faca refatoracao oportunista nem renomeacao fora do blocker.
6. Nao altere P0/exactly-once a menos que o blocker prove necessidade.
7. NAO implemente bypass de CAPTCHA/challenge.
8. NAO execute candidatura real: `--submit` pertence somente ao supervisor.
9. NAO invente dados do candidato (pessoal, legal, salario, autorizacao).
10. Deixe a arvore com SOMENTE a correcao e a regressao deste blocker.
"""


def write_blocker_bundle(root: Path, context: dict[str, Any], *, iteration: int) -> Path:
    directory = root / f"iteration-{iteration:03d}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "context.json").write_text(json.dumps(context, indent=2, ensure_ascii=False), encoding="utf-8")
    (directory / "prompt.md").write_text(PROMPT_TEMPLATE.format(context_file=directory / "context.json", **context), encoding="utf-8")
    return directory


def assert_fixer_cannot_submit(command: str) -> None:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise SupervisorError("invalid fixer command") from exc
    if argv != list(ALLOWED_FIX_COMMAND):
        raise SupervisorError("fixer command must be exactly: codex exec --file {prompt_file}")


def run_fixer(command: str, prompt_file: Path, *, timeout: int) -> int:
    assert_fixer_cannot_submit(command)
    argv = [part.replace("{prompt_file}", str(prompt_file)) for part in shlex.split(command)]
    completed = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=timeout, check=False)
    return completed.returncode


# -- git e testes ---------------------------------------------------------------


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)


def changed_files() -> list[str]:
    result = git("status", "--porcelain")
    if result.returncode != 0:
        raise SupervisorError(f"git status failed: {result.stderr[-500:]}")
    return [line[3:].strip() for line in result.stdout.splitlines() if line.strip()]


def forbidden_in_diff(files: Sequence[str]) -> list[str]:
    offenders: list[str] = []
    for path in files:
        lowered = path.casefold()
        if any(pattern in lowered for pattern in FORBIDDEN_COMMIT_PATTERNS):
            offenders.append(path)
    return offenders


def workspace_snapshot() -> dict[str, str]:
    result = git("status", "--porcelain")
    if result.returncode != 0:
        raise SupervisorError(f"git status failed: {result.stderr[-500:]}")
    snapshot: dict[str, str] = {}
    for path in changed_files():
        file_path = REPO / path
        snapshot[path] = hashlib.sha256(file_path.read_bytes()).hexdigest() if file_path.is_file() else "<missing>"
    return snapshot


def new_fixer_paths(baseline: dict[str, str]) -> list[str]:
    changed: list[str] = []
    for path in changed_files():
        file_path = REPO / path
        current = hashlib.sha256(file_path.read_bytes()).hexdigest() if file_path.is_file() else "<missing>"
        if path in baseline:
            if baseline[path] != current:
                raise SupervisorError(f"fixer modified pre-existing worktree path: {path}")
            continue
        changed.append(path)
    offenders = forbidden_in_diff(changed)
    if offenders:
        raise SupervisorError(f"fixer touched forbidden paths: {offenders}")
    if any(not path.startswith(ALLOWED_FIX_PATH_PREFIXES) for path in changed):
        raise SupervisorError(f"fixer touched non-source paths: {changed}")
    return changed


def run_tests(selector: Sequence[str], *, timeout: int) -> tuple[bool, str]:
    argv = [sys.executable, "-m", "pytest", *selector, "-q", "-p", "no:randomly"]
    completed = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=timeout, check=False)
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode == 0, output[-4000:]


def commit(message: str, paths: Sequence[str]) -> str:
    if not paths or any(not path.startswith(ALLOWED_FIX_PATH_PREFIXES) for path in paths):
        raise SupervisorError("refusing to commit an empty or non-source fixer diff")
    existing = git("diff", "--cached", "--name-only")
    if existing.returncode != 0:
        raise SupervisorError(f"git diff --cached failed: {existing.stderr[-500:]}")
    if existing.stdout.strip():
        raise SupervisorError("refusing to commit with pre-existing staged paths")
    staged = git("add", "--", *paths)
    if staged.returncode != 0:
        raise SupervisorError(f"git add failed: {staged.stderr[-500:]}")
    staged_paths = git("diff", "--cached", "--name-only")
    if staged_paths.returncode != 0:
        raise SupervisorError(f"git diff --cached failed after add: {staged_paths.stderr[-500:]}")
    actual = {line.strip() for line in staged_paths.stdout.splitlines() if line.strip()}
    expected = set(paths)
    if actual != expected:
        raise SupervisorError(f"staged paths differ from fixer paths: expected={sorted(expected)} actual={sorted(actual)}")
    committed = git("commit", "-q", "-m", message)
    if committed.returncode != 0:
        raise SupervisorError(f"git commit failed: {committed.stderr[-500:]}")
    head = git("rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        raise SupervisorError("git rev-parse HEAD failed after commit")
    return head.stdout.strip()


# -- execucao real --------------------------------------------------------------


def run_real_application(args: argparse.Namespace) -> RunResult:
    argv = [
        sys.executable, "-m", "jobsearch_agent.cli", "apply-to-completion", args.job_id,
        "--db", str(args.db),
        "--timeout", str(args.submission_timeout),
        "--captcha-wait", str(args.captcha_wait),
    ]
    if args.headless is False:
        argv.append("--no-headless")
    if args.real_profile:
        argv.append("--real-profile")
    argv.append("--submit")

    try:
        completed = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=args.timeout, check=False)
    except subprocess.TimeoutExpired as expired:
        return RunResult(exit_code=124, stdout_tail=str(expired.stdout or "")[-2000:], timed_out=True)

    payload: dict[str, Any] = {}
    raw = completed.stdout or ""
    start = raw.find("{")
    if start >= 0:
        try:
            parsed = json.loads(raw[start:])
            payload = dict(parsed.get("result", parsed)) if isinstance(parsed, dict) else {}
        except ValueError:
            payload = {}
    return RunResult(exit_code=completed.returncode, payload=payload, stdout_tail=raw[-2000:])


def recover_if_stranded(database: Database, durable: Durable) -> tuple[str, str]:
    """Uma unica tentativa de recovery, depois que o processo filho terminou."""
    if durable.state != ApplicationState.SUBMITTING.value:
        return durable.state, ""
    try:
        branch = database.recover_stranded_submission(durable.application_id)
    except ApplicationConflict as exc:
        return "corrupt", str(exc)
    if branch == Database.RECOVERY_PRE_WRITE:
        return ApplicationState.REVIEW_REACHED.value, "pre_write"
    if branch == Database.RECOVERY_UNKNOWN:
        return ApplicationState.SUBMIT_UNKNOWN.value, "unknown"
    return branch, "noop"


# -- journal --------------------------------------------------------------------


def journal_append(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


# -- supervisor -----------------------------------------------------------------


def supervisor_loop(
    *,
    database: Database,
    args: argparse.Namespace,
    run_application: Callable[[], RunResult] | None = None,
    fix: Callable[[Path, int], int] | None = None,
    test: Callable[[Sequence[str]], tuple[bool, str]] | None = None,
) -> int:
    """Loop run -> blocker -> fix -> test -> commit -> run. Nao e loop de submissao."""
    journal = Path(args.journal_dir) / "journal.jsonl"
    iteration = 0
    fixes_for_blocker = 0
    active_blocker: tuple[str, str] | None = None
    while iteration < args.max_iterations:
        iteration += 1
        sha = git("rev-parse", "HEAD").stdout.strip()
        execute = run_application or (lambda: run_real_application(args))
        result = execute()
        durable = inspect_database(database, args.application_id)

        recovery_outcome: tuple[str, str] | None = None
        if durable.state == ApplicationState.SUBMITTING.value:
            _new_state, branch = recover_if_stranded(database, durable)
            durable = inspect_database(database, args.application_id)
            if branch in {"unknown", "corrupt"}:
                journal_append(journal, {"iteration": iteration, "sha": sha, "classification": POST_WRITE_STOP, "reason": f"recovery:{branch}"})
                print(f"[iter {iteration}] HARD STOP: recovery {branch}")
                return 3
            if branch == "pre_write":
                recovery_outcome = (PREWRITE_BLOCKER, "recovery confirmou ausencia de escrita")

        outcome, reason = recovery_outcome or classify(result, durable)
        journal_append(
            journal,
            {
                "iteration": iteration,
                "sha": sha,
                "classification": outcome,
                "reason": reason,
                "state": durable.state,
                "status": result.status,
                "exit_code": result.exit_code,
            },
        )
        print(f"[iter {iteration}] {outcome}: {reason} (state={durable.state}, status={result.status})")

        if outcome == SUCCESS:
            return 0
        if outcome in {HUMAN_REQUIRED, POST_WRITE_STOP}:
            return 3 if outcome == POST_WRITE_STOP else 4

        # PREWRITE_BLOCKER: contexto -> fixer -> validacao -> commit -> run de novo.
        blocker_key = (durable.state, "prewrite")
        if blocker_key != active_blocker:
            active_blocker = blocker_key
            fixes_for_blocker = 0
        fixes_for_blocker += 1
        if fixes_for_blocker > args.max_fix_attempts:
            print(f"[iter {iteration}] HARD STOP: {args.max_fix_attempts} tentativas de fix neste blocker")
            return 5

        context = build_context(
            iteration=iteration, sha=sha, branch=args.branch, job_id=args.job_id,
            application_id=args.application_id, result=result, durable=durable,
            reason=reason, changed_files=changed_files(),
        )
        directory = write_blocker_bundle(Path(args.journal_dir), context, iteration=iteration)
        print(f"[iter {iteration}] contexto: {directory}/context.json")

        if fix is None:
            print("[iter {i}] sem --fix-command: parando para correcao manual".format(i=iteration))
            return 6

        baseline = workspace_snapshot()
        fixer_exit = fix(directory / "prompt.md", args.timeout)
        journal_append(journal, {"iteration": iteration, "sha": sha, "fixer_exit": fixer_exit})
        if fixer_exit != 0:
            print(f"[iter {iteration}] fixer falhou ({fixer_exit}): parando")
            return 6

        try:
            touched = new_fixer_paths(baseline)
        except SupervisorError as exc:
            print(f"[iter {iteration}] HARD STOP: {exc}")
            return 7
        if not touched:
            print(f"[iter {iteration}] fixer nao alterou nada: parando")
            return 6

        if git("diff", "--check").returncode != 0:
            print(f"[iter {iteration}] HARD STOP: git diff --check sujo")
            return 9

        run_test = test or (lambda selector: run_tests(selector, timeout=args.tests_timeout))
        focused_ok, focused_out = run_test(["tests"]) if args.focused_tests == ["tests"] else run_test(args.focused_tests)
        if not focused_ok:
            journal_append(journal, {"iteration": iteration, "sha": sha, "focused_failed": True})
            print(f"[iter {iteration}] HARD STOP: teste focado vermelho")
            return 9
        suite_ok, suite_out = run_test(["tests"])
        if not suite_ok:
            journal_append(journal, {"iteration": iteration, "sha": sha, "suite_failed": True})
            print(f"[iter {iteration}] HARD STOP: suite vermelha")
            return 9

        try:
            new_sha = commit(f"fix(real-apply): iteration {iteration} {reason[:60]}", touched)
        except SupervisorError as exc:
            print(f"[iter {iteration}] HARD STOP: {exc}")
            return 8
        journal_append(journal, {"iteration": iteration, "sha": sha, "new_sha": new_sha})
        print(f"[iter {iteration}] commit {new_sha[:12]}")
        if args.push:
            pushed = git("push", "origin", args.branch)
            if pushed.returncode != 0:
                print(f"[iter {iteration}] HARD STOP: git push failed: {pushed.stderr[-500:]}")
                return 8
            remote = git("ls-remote", "origin", f"refs/heads/{args.branch}")
            remote_sha = remote.stdout.split()[0] if remote.returncode == 0 and remote.stdout.split() else ""
            if remote.returncode != 0 or remote_sha != new_sha:
                print(f"[iter {iteration}] HARD STOP: remote ref verification failed")
                return 8

    print(f"[stop] limite de iteracoes atingido ({args.max_iterations})")
    return 5


def acquire_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise SupervisorError(f"outro supervisor ativo (lock: {path})") from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Supervisor de self-improvement do REAL-APPLY-001")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--db", default=str(REPO / "data" / "jobsearch.db"))
    parser.add_argument("--branch", default="real-apply-001")
    parser.add_argument("--journal-dir", default=str(REPO / "data" / "self-improve"))
    parser.add_argument("--fix-command", default="")
    parser.add_argument("--focused-tests", nargs="*", default=["tests"])
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--max-fix-attempts", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--tests-timeout", type=int, default=1800)
    parser.add_argument("--submission-timeout", type=float, default=45.0)
    parser.add_argument("--captcha-wait", type=float, default=0.0)
    parser.add_argument("--real-profile", action="store_true")
    parser.add_argument("--headless", dest="headless", action="store_true", default=False)
    parser.add_argument("--push", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    lock = Path(args.journal_dir) / "supervisor.lock"
    handle = acquire_lock(lock)
    database = Database(args.db)
    try:
        os.write(handle, str(os.getpid()).encode())
        fixer = (lambda prompt, timeout: run_fixer(args.fix_command, prompt, timeout=timeout)) if args.fix_command else None
        return supervisor_loop(database=database, args=args, fix=fixer)
    finally:
        os.close(handle)
        lock.unlink(missing_ok=True)
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
