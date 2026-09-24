from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import replace
import json
import threading
import time

import pytest

pytest.importorskip("playwright")
from pypdf import PdfWriter
from jobsearch_agent.browser import PlaywrightFormFiller, PlaywrightSessionManager
from jobsearch_agent.ats import GreenhouseAdapter
from jobsearch_agent.execution import build_execution_plan
from jobsearch_agent.inspector import ATSInspector
from jobsearch_agent.models import ApplicationContext, ApplicationPolicy, CandidatePreferences
from jobsearch_agent.orchestrator import LiveApplicationOrchestrator
from jobsearch_agent.profile import load_profile
from jobsearch_agent.qa import AnswerKnowledgeBase


def test_local_chromium_dry_run_inspects_fills_uploads_and_screenshots(tmp_path: Path):
    artifact = tmp_path / "resume.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with artifact.open("wb") as handle:
        writer.write(handle)

    html = """
    <form id="application">
      <label for="name">Name</label><input id="name" name="name" required>
      <label for="resume">Resume</label><input id="resume" name="resume" type="file" accept="application/pdf" required>
    </form>
    """
    manager = PlaywrightSessionManager(allowed_hosts={"example.com"})
    manager.start()
    try:
        page = manager.page
        assert manager.context is not None
        assert hasattr(manager.context, "route_web_socket")
        page.set_content(html)
        inspected = ATSInspector().inspect_page(page, form_selector="#application")
        inspected.form.artifact_root = str(tmp_path)
        for field in inspected.form.fields:
            if field.key == "name":
                field.value = "Candidate"
            if field.key == "resume":
                field.attachment_path = str(artifact)
        application_context = ApplicationContext(
            application_id="application-browser-fixture",
            job_id="job-browser-fixture",
            fit={"blockers": []},
            validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
            form=inspected.form,
            policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "manual"}),
        )
        plan = build_execution_plan(application_context, inspected.bindings)
        audit_dir = tmp_path / "browser"
        preflight_result = page.evaluate("""async () => {
            try { await fetch('https://example.com/write', {method: 'POST', body: 'blocked'}); return 'sent'; }
            catch (error) { return 'blocked'; }
        }""")
        result = PlaywrightFormFiller().fill(manager, application_context, plan, inspected.bindings, audit_dir=audit_dir)
        assert page.locator("#name").input_value() == "Candidate"
        assert page.locator("#resume").evaluate("element => element.files.length") == 1
        assert result.stopped_before_submit is True
        assert preflight_result == "blocked"
        assert (audit_dir / "screenshot-before.png").is_file()
        assert (audit_dir / "screenshot-after.png").is_file()
        assert (audit_dir / "dry-run-report.json").is_file()
        assert (audit_dir.stat().st_mode & 0o777) == 0o700
        assert (audit_dir / "screenshot-before.png").stat().st_mode & 0o777 == 0o600
        assert (audit_dir / "screenshot-after.png").stat().st_mode & 0o777 == 0o600
        report = (audit_dir / "dry-run-report.json").read_text(encoding="utf-8")
        assert '"network_guard_active": true' in report
        assert '"blocked_write_count": 1' in report
        assert '"pending_read_count": 0' in report
        assert not hasattr(PlaywrightFormFiller(), "submit")
        assert manager.network_guard is not None
        assert manager.context is not None
        assert any(event.method == "POST" and not event.allowed for event in manager.network_guard.events)
    finally:
        manager.close()


def test_greenhouse_conditional_get_is_pending_before_dom_change(tmp_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/locations"):
                time.sleep(0.4)
                body = b'{"locations":["Sao Paulo"]}'
            else:
                body = b'''<!doctype html><form id="application_form">
                  <label for="relocation">Are you willing to relocate?</label>
                  <select id="relocation" name="job_application[relocation]" onchange="fetch('/locations?relocation=yes').then(() => { const input = document.createElement('input'); input.id = 'relocation_location'; input.name = 'job_application[relocation_location]'; input.required = true; document.querySelector('form').append(input); })">
                    <option value="">Choose</option><option value="yes">Yes</option><option value="no">No</option>
                  </select>
                </form>'''
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if self.path.startswith("/locations") else "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class LocalFixtureSession(PlaywrightSessionManager):
        def __init__(self, origin):
            super().__init__(allowed_hosts={"127.0.0.1"})
            self.origin = origin
            self.pending_samples = []

        def _guard_route(self, route):
            if route.request.url.startswith(self.origin):
                allowed = self.network_guard.inspect(route.request)
                if allowed:
                    self.network_guard.begin_read(route.request)
                    self.pending_samples.append(self.network_guard.pending_read_count)
                    route.continue_()
                else:
                    route.abort("blockedbyclient")
                return
            super()._guard_route(route)

    origin = f"http://127.0.0.1:{server.server_port}"
    manager = LocalFixtureSession(origin)
    manager.start()
    try:
        page = manager.page
        page.goto(origin + "/application")
        adapter_result = GreenhouseAdapter().inspect(page.content(), origin + "/application")
        relocation = next(field for field in adapter_result.form.fields if field.semantic_type == "relocation")
        relocation.value = "Yes"
        adapter_result.form.artifact_root = str(tmp_path)
        context = ApplicationContext(
            application_id="application-conditional",
            job_id="job-conditional",
            fit={"blockers": []},
            validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
            form=adapter_result.form,
            policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "manual"}),
        )
        plan = build_execution_plan(context, adapter_result.bindings)
        started = time.monotonic()
        result = PlaywrightFormFiller().fill(manager, context, plan, adapter_result.bindings)
        assert time.monotonic() - started >= 0.35
        assert max(manager.pending_samples) >= 1
        assert manager.network_guard.pending_read_count == 0
        assert result.status == "FORM_CHANGED"
        assert len(result.operations) == 1
        assert page.locator("#relocation_location").count() == 1
    finally:
        manager.close()
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("options", "expected_status", "expected_reason"),
    [
        (["Canada", "Cameroon"], "COMPLETED", ""),
        # Duas opcoes DIFERENTES que casam com o mesmo texto: ambiguidade real.
        # (A mesma opcao repetida nao e ambiguidade — o widget re-renderiza a cada
        # tecla e repete a lista.)
        (["Canada East", "Canada West"], "UNSUPPORTED_FORM", "AMBIGUOUS_COMBOBOX_OPTION"),
        (["Cameroon"], "UNSUPPORTED_FORM", "OPTION_NOT_FOUND_COMBOBOX_OPTION"),
    ],
)
def test_single_async_greenhouse_combobox_waits_for_get_and_selects_one_exact_option(
    tmp_path: Path, options, expected_status, expected_reason
):
    fixture = Path(__file__).parent / "fixtures/greenhouse/async-combobox.html"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/countries"):
                time.sleep(0.4)
                body = json.dumps(options).encode("utf-8")
                content_type = "application/json"
            else:
                body = fixture.read_bytes()
                content_type = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class LocalFixtureSession(PlaywrightSessionManager):
        def __init__(self, origin):
            super().__init__(allowed_hosts={"127.0.0.1"})
            self.origin = origin
            self.pending_samples = []

        def _guard_route(self, route):
            if route.request.url.startswith(self.origin):
                allowed = self.network_guard.inspect(route.request)
                if allowed:
                    self.network_guard.begin_read(route.request)
                    self.pending_samples.append(self.network_guard.pending_read_count)
                    route.continue_()
                else:
                    route.abort("blockedbyclient")
                return
            super()._guard_route(route)

    origin = f"http://127.0.0.1:{server.server_port}"
    manager = LocalFixtureSession(origin)
    manager.start()
    try:
        page = manager.page
        page.goto(origin + "/application")
        inspected = GreenhouseAdapter().inspect(page.content(), origin + "/application")
        country = next(field for field in inspected.form.fields if field.key == "job_application[country]")
        country.value = "Canada"
        inspected.form.artifact_root = str(tmp_path)
        context = ApplicationContext(
            application_id="application-combobox",
            job_id="job-combobox",
            fit={"blockers": []},
            validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
            form=inspected.form,
            policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "manual"}),
        )
        plan = build_execution_plan(context, inspected.bindings)
        started = time.monotonic()
        audit_dir = tmp_path / "audit"
        result = PlaywrightFormFiller().fill(manager, context, plan, inspected.bindings, audit_dir=audit_dir)
        assert time.monotonic() - started >= 0.35
        assert result.status == expected_status, result.reason
        assert len(result.operations) == (1 if expected_status == "COMPLETED" else 0)
        if expected_status == "UNSUPPORTED_FORM":
            assert result.reason.startswith(expected_reason)
            report = json.loads((audit_dir / "dry-run-report.json").read_text(encoding="utf-8"))
            assert report["result"] == "UNSUPPORTED_FORM"
            assert report["reason"] == result.reason
        else:
            assert page.locator("#country").input_value() == "Canada"
        assert max(manager.pending_samples) >= 1
        assert manager.network_guard.pending_read_count == 0
        assert result.blocked_write_count == 0
    finally:
        manager.close()
        server.shutdown()
        thread.join(timeout=2)


def test_live_orchestrator_fills_and_uploads_a_real_page_without_submitting(tmp_path: Path):
    """Real browser, real HTTP: navigate, fill, upload the resume, never POST."""
    posted: list[str] = []
    form_html = """<!doctype html>
    <form id="application_form">
      <label for="email">Email</label>
      <input id="email" name="job_application[email]" type="email" required>
      <label for="resume">Resume</label>
      <input id="resume" name="job_application[resume]" type="file" accept="application/pdf" required>
      <button type="submit">Submit application</button>
    </form>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = form_html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            posted.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class LocalLiveSession(PlaywrightSessionManager):
        def __init__(self, origin):
            super().__init__(allowed_hosts={"127.0.0.1"})
            self.origin = origin

        def _guard_route(self, route):
            if route.request.url.startswith(self.origin):
                if self.network_guard.inspect(route.request):
                    self.network_guard.begin_read(route.request)
                    route.continue_()
                else:
                    route.abort("blockedbyclient")
                return
            super()._guard_route(route)

        def open(self, url):  # test-only: loopback bypasses the public-host policy
            self.page.goto(url, wait_until="domcontentloaded")

    resume = tmp_path / "resume.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with resume.open("wb") as handle:
        writer.write(handle)

    origin = f"http://127.0.0.1:{server.server_port}"
    profile = replace(load_profile("profile/career_profile.yaml"), demo=False)
    manager = LocalLiveSession(origin)
    manager.start()
    try:
        context = ApplicationContext(
            application_id="application-live-browser",
            job_id="job-live-browser",
            fit={"blockers": []},
            validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
            policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "manual"}),
        )
        orchestrator = LiveApplicationOrchestrator(
            GreenhouseAdapter(),
            profile,
            CandidatePreferences(),
            AnswerKnowledgeBase([]),
            artifact_root=str(tmp_path),
            default_resume=str(resume),
        )
        result = orchestrator.run(manager, context, origin + "/application")

        assert result.status == "FILLED_REVIEW_REQUIRED"
        assert manager.page.locator("#email").input_value() != ""
        assert manager.page.locator("#resume").evaluate("element => element.files.length") == 1
        assert result.form_fingerprint
        assert orchestrator.filler is not None
        assert not hasattr(orchestrator.filler, "submit")
        assert posted == [], "the guarded session must never reach a real submit"
        assert manager.network_guard is not None
        assert manager.network_guard.blocked_writes == []
    finally:
        manager.close()
        server.shutdown()
        thread.join(timeout=2)


def test_apply_live_runs_the_real_orchestrator_with_a_candidate_profile(tmp_path: Path, monkeypatch):
    """Integração real do pipeline, SEM stub de orchestrator.

    Um P0 anterior trocou o CareerProfile por um ProviderProfile na chamada do
    `LiveApplicationOrchestrator`. Os testes com `StubOrchestrator` aceitavam
    `*_args`, então nada quebrava. Aqui o orchestrator é o real: ele usa
    `profile.demo`, `profile.experiences` e `profile.candidate_preferences`, e
    falharia imediatamente se recebesse o objeto errado.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    from jobsearch_agent import pipeline
    from jobsearch_agent.config import Settings
    from jobsearch_agent.models import Job
    from jobsearch_agent.persistence import Database
    from jobsearch_agent.profile import load_profile
    from jobsearch_agent.sources import canonical_job_key

    root = Path(__file__).parents[1]
    form_html = """<!doctype html>
    <form id="application-form" method="post" action="/apply">
      <label for="name">Full name</label><input id="name" name="name" required>
      <label for="email">Email</label><input id="email" name="email" type="email" required>
      <label for="resume">Resume</label><input id="resume" name="resume" type="file">
      <button type="submit">SUBMIT APPLICATION</button>
    </form>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = form_html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"

    class LoopbackSession(PlaywrightSessionManager):
        def _guard_route(self, route):
            if str(route.request.url).startswith(origin):
                if self.network_guard.inspect(route.request):
                    self.network_guard.begin_read(route.request)
                    route.continue_()
                else:
                    route.abort("blockedbyclient")
                return
            super()._guard_route(route)

        def open(self, url):
            self.page.goto(url, wait_until="domcontentloaded")

    # perfil de candidato real (fixture) marcado como nao-demo
    profile_path = tmp_path / "career_profile.yaml"
    profile_path.write_text(
        (root / "profile/career_profile.yaml").read_text().replace("demo: true", "demo: false"),
        encoding="utf-8",
    )
    # Unico seam alem do loopback: a identidade do provider. Num host local nao
    # ha como reconhecer o ATS pela URL; o orchestrator continua sendo o real.
    from jobsearch_agent.ats import LeverAdapter

    real_adapter_for = pipeline.adapter_for
    monkeypatch.setattr(
        pipeline,
        "adapter_for",
        lambda url, html="": LeverAdapter() if "127.0.0.1" in url else real_adapter_for(url, html),
    )
    monkeypatch.setattr(pipeline, "PlaywrightSessionManager", LoopbackSession)

    settings = Settings.from_args(
        root,
        db=str(tmp_path / "jobs.db"),
        artifacts=str(tmp_path / "artifacts"),
        profile=str(profile_path),
        facts=str(root / "profile/locked_facts.yaml"),
        answers=str(root / "profile/answers.yaml"),
        application_policy=str(root / "profile/application_policy.yaml"),
    )

    artifact_dir = tmp_path / "artifacts" / "job-lever-local"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with (artifact_dir / "resume.pdf").open("wb") as handle:
        writer.write(handle)

    def fake_prepare(_settings, _job_id, language_override=None):
        return {
            "fit": {"blockers": []},
            "resume": {"id": "resume-local"},
            "validation": {"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
            "artifacts": str(artifact_dir),
        }

    monkeypatch.setattr(pipeline, "prepare", fake_prepare)

    db = Database(settings.resolve(settings.db_path))
    try:
        job = Job(
            id="job-lever-local",
            source="lever",
            external_id="local-1",
            company="acme",
            title="Frontend Engineer",
            description="Build web apps.",
            url=origin + "/apply",
        )
        db.save_job(job, canonical_job_key(job), {})
    finally:
        db.close()

    result = pipeline.apply_live(settings, "job-lever-local")
    server.shutdown()

    # Sem excecao e com status de dominio: o orchestrator real rodou.
    assert result["status"] == "FILLED_REVIEW_REQUIRED", result.get("error")
    assert result["network_guard_active"] is True
    assert result["write_policy"] == "deny_all"
    assert result["upload_writes_used"] == 0
    assert load_profile(profile_path).demo is False

    # O fixture declara action="/apply" (relativo). O destino derivado dele
    # precisa sair absoluto: e exatamente o caminho que o submit percorre e
    # que nao era exercitado com submit=False.
    from urllib.parse import urlsplit

    from jobsearch_agent.submission import submission_destination

    db = Database(settings.resolve(settings.db_path))
    try:
        stored = db.get_application_form(result["application_id"])
    finally:
        db.close()
    assert stored["action"] == "/apply"
    assert stored["method"] == "POST"
    destination = submission_destination(
        "lever", "acme", "local-1", origin + "/apply", stored["action"]
    )
    parts = urlsplit(destination)
    assert parts.scheme == "http" and parts.netloc, destination


def test_native_form_submit_reaches_the_network_only_with_an_armed_permit():
    """O bloqueio client-side nao pode impedir uma submissao autorizada.

    O script de init cancelava todo `submit` nativo durante toda a sessao, para
    sempre. Em ATS que enviam o formulario pela API nativa do HTML (o Lever, por
    exemplo) o clique em "Submit application" morria dentro do JavaScript: o
    guard nunca via requisicao alguma e a submissao nao acontecia, sem erro
    visivel. Quem decide agora e o NetworkWriteGuard.
    """
    from jobsearch_agent.browser import AuthorizedWrite

    posted: list[str] = []
    form_html = b"""<!doctype html>
    <form id="application" method="post" action="/apply">
      <input name="name" value="Candidate">
      <button type="submit">Submit application</button>
    </form>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(form_html)))
            self.end_headers()
            self.wfile.write(form_html)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            posted.append(self.path)
            body = b"<html><body>Thanks for applying</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    class LoopbackSession(PlaywrightSessionManager):
        def __init__(self, loopback_origin):
            super().__init__(allowed_hosts={"127.0.0.1"})
            self.origin = loopback_origin

        def _guard_route(self, route):
            if str(route.request.url).startswith(self.origin):
                if self.network_guard.inspect(route.request):
                    self.network_guard.begin_read(route.request)
                    route.continue_()
                else:
                    route.abort("blockedbyclient")
                return
            super()._guard_route(route)

        def open(self, url):
            self.page.goto(url, wait_until="domcontentloaded")

    manager = LoopbackSession(origin)
    manager.start()
    try:
        manager.open(origin + "/application")

        # Sem permit armado o submit nativo nao pode produzir escrita alguma.
        manager.page.locator("button[type=submit]").click()
        manager.page.wait_for_timeout(500)
        assert posted == [], "unarmed session must not reach the network"

        # Com o permit armado a requisicao existe e e auditada pelo guard.
        manager.arm_writes([
            AuthorizedWrite(
                application_id="application-native-submit",
                submission_intent_id="intent-native-submit",
                origin=origin,
                path_pattern=r"^/apply$",
                method="POST",
                max_writes=1,
            )
        ])
        manager.page.goto(origin + "/application", wait_until="domcontentloaded")
        manager.page.locator("button[type=submit]").click()
        manager.page.wait_for_timeout(800)
        assert posted == ["/apply"], f"authorized submit must reach the server: {posted}"
        assert manager.network_guard is not None
        assert manager.network_guard.authorized_writes_used == 1
    finally:
        manager.close()
        server.shutdown()
        thread.join(timeout=2)


def test_ashby_style_read_only_post_is_blocked_unless_an_inspection_permit_is_armed():
    """Fixture real: a SPA busca o schema por POST e o guard decide pelo conteudo.

    Sem permissao a pagina nao consegue buscar o formulario (e o board fica
    inalcancavel de proposito). Com a permissao de inspecao armada, a MESMA
    requisicao passa — e uma mutacao no mesmo endpoint continua bloqueada.
    """
    from jobsearch_agent.inspection import AuthorizedInspectionRequest

    origin_holder: list[str] = []
    posted: list[str] = []
    job_id = "d3bc1ced-3ce4-4086-a050-555055dbb1ff"
    graphql = (
        "query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) { "
        "jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, "
        "jobPostingId: $jobPostingId) { id } }"
    )

    def page_html() -> bytes:
        return f"""<!doctype html><html><body><div id="out">PENDING</div>
        <script>
          const body = (query) => JSON.stringify({{
            operationName: 'ApiJobPosting',
            variables: {{organizationHostedJobsPageName: 'linear', jobPostingId: '{job_id}'}},
            query: query || {json.dumps(graphql)}
          }});
          window.run = (query) => fetch('/api/non-user-graphql?op=ApiJobPosting', {{
            method: 'POST', headers: {{'content-type': 'application/json'}}, body: body(query)
          }}).then(r => r.json()).then(d => {{
            document.getElementById('out').textContent = 'FIELDS:' + (d.data.fields || []).join(',');
          }}).catch(() => {{ document.getElementById('out').textContent = 'BLOCKED'; }});
        </script></body></html>""".encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = page_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length).decode("utf-8")
            posted.append(payload)
            body = json.dumps({"data": {"fields": ["name", "email", "resume"]}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    origin_holder.append(origin)

    class LoopbackSession(PlaywrightSessionManager):
        def __init__(self, loopback_origin):
            super().__init__(allowed_hosts={"127.0.0.1"})
            self.origin = loopback_origin

        def _guard_route(self, route):
            if str(route.request.url).startswith(self.origin):
                if self.network_guard.inspect(route.request):
                    self.network_guard.begin_read(route.request)
                    route.continue_()
                else:
                    route.abort("blockedbyclient")
                return
            super()._guard_route(route)

        def open(self, url):
            self.page.goto(url, wait_until="domcontentloaded")

    manager = LoopbackSession(origin)
    manager.start()
    try:
        manager.open(origin + "/application")
        # 1. Sem permissao: o POST read-only e recusado e a pagina nao recebe schema.
        manager.page.evaluate("() => window.run()")
        manager.page.wait_for_function("() => document.getElementById('out').textContent !== 'PENDING'")
        assert manager.page.locator("#out").inner_text() == "BLOCKED"
        assert posted == []

        # 2. Com a permissao de inspecao: a mesma requisicao passa.
        manager.arm_inspections([
            AuthorizedInspectionRequest(
                application_id="application-ashby-fixture",
                provider="ashby",
                origin=origin,
                path_pattern=r"^/api/non-user-graphql$",
                method="POST",
                operation_names=("ApiJobPosting",),
                expected_variables=(
                    ("jobPostingId", job_id),
                    ("organizationHostedJobsPageName", "linear"),
                ),
                max_requests=2,
            )
        ])
        manager.page.evaluate("() => window.run()")
        manager.page.wait_for_function(
            "() => document.getElementById('out').textContent.startsWith('FIELDS:')"
        )
        assert manager.page.locator("#out").inner_text() == "FIELDS:name,email,resume"
        assert len(posted) == 1
        assert manager.network_guard.inspections_used == 1
        # Nada disso consumiu credito de escrita, e o submit segue bloqueado.
        assert manager.network_guard.authorized_writes_used == 0

        # 3. Mutacao no MESMO endpoint continua recusada, com permissao armada.
        manager.page.evaluate("() => window.run('mutation ApiJobPosting { drop { id } }')")
        manager.page.wait_for_function("() => document.getElementById('out').textContent === 'BLOCKED'")
        assert len(posted) == 1
        assert manager.network_guard.inspections_used == 1
    finally:
        manager.close()
        server.shutdown()
        thread.join(timeout=2)
