from pathlib import Path

import pytest

from jobsearch_agent.application import evaluate_safety_gate
from jobsearch_agent.browser import BrowserSessionError, DryRunBrowserExecutor, NetworkWriteGuard, OptionSelectionError, PlaywrightFormFiller, PlaywrightSessionManager, choose_option_index, validate_navigation_url
from jobsearch_agent.execution import DryRunExecutionPlan, ExecutionAction, ExecutionPlan, ExecutionPlanError, LiveApplicationPlan, build_execution_plan, validate_live_application_plan
from jobsearch_agent.forms import validate_application_field, validate_application_form
from jobsearch_agent.inspector import ATSInspector, DOMFieldBinding, FormBindings, InspectionError, validate_bindings_against_html
from jobsearch_agent.models import ApplicationContext, ApplicationField, ApplicationForm, ApplicationPolicy, ApplicationState
from jobsearch_agent.profile import load_preferences, load_profile
from jobsearch_agent.qa import AnswerKnowledgeBase


ROOT = Path(__file__).parents[1]


def _valid_pdf() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 10] >>",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{index} 0 obj\n".encode())
        body.extend(obj + b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    body.extend("".join(f"{offset:010d} 00000 n \n" for offset in offsets[1:]).encode())
    body.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(body)


def _ready_context(form: ApplicationForm) -> ApplicationContext:
    return ApplicationContext(
        application_id="application-1",
        job_id="job-1",
        fit={"blockers": []},
        validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        form=form,
        policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "manual"}),
    )


def _bindings_for(form: ApplicationForm) -> FormBindings:
    controls = {"textarea": "textarea", "select": "select", "radio": "radio", "checkbox": "checkbox", "file": "file"}
    fields = [DOMFieldBinding(field.key, f"#{field.key}", controls.get(field.field_type.casefold(), "text"), option_values={option: option for option in field.options}) for field in form.fields]
    return FormBindings(form.form_id, fields)


def test_file_field_requires_existing_artifact_and_accepted_extension(tmp_path: Path):
    missing = ApplicationField("resume", "Resume", field_type="file", semantic_type="resume", required=True, accepted_types=["pdf"])
    assert validate_application_field(missing).valid is False
    artifact = tmp_path / "resume.pdf"
    artifact.write_bytes(_valid_pdf())
    valid = ApplicationField("resume", "Resume", field_type="file", semantic_type="resume", required=True, attachment_path=str(artifact), accepted_types=["pdf"])
    assert validate_application_field(valid, str(tmp_path)).valid is True
    value_only = ApplicationField("resume", "Resume", field_type="file", required=True, value=str(artifact), accepted_types=["pdf"])
    assert validate_application_field(value_only, str(tmp_path)).valid is False
    wrong = tmp_path / "resume.txt"
    wrong.write_text("fixture", encoding="utf-8")
    invalid_type = ApplicationField("resume", "Resume", field_type="file", required=True, attachment_path=str(wrong), accepted_types=["pdf"])
    assert validate_application_field(invalid_type, str(tmp_path)).valid is False
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(_valid_pdf())
    external = ApplicationField("resume", "Resume", field_type="file", required=True, attachment_path=str(outside), accepted_types=["pdf"])
    assert validate_application_field(external, str(tmp_path)).valid is False


def test_safety_gate_uses_needs_artifact_for_missing_upload(tmp_path: Path):
    form = ApplicationForm("form-1", artifact_root=str(tmp_path), fields=[ApplicationField("resume", "Resume", field_type="file", required=True, accepted_types=["pdf"])])
    readiness = evaluate_safety_gate(_ready_context(form))
    assert readiness.decision == ApplicationState.NEEDS_ARTIFACT
    assert "invalid_artifact:resume" in readiness.blockers


def test_form_validation_rejects_unknown_option_and_required_checkbox():
    form = ApplicationForm(
        "form-1",
        fields=[
            ApplicationField("sponsorship", "Sponsorship", field_type="radio", options=["Yes", "No"], required=True, value="Brazil"),
            ApplicationField("consent", "Consent", field_type="checkbox", semantic_type="checkbox_boolean", required=True, value="false"),
        ],
    )
    result = validate_application_form(form)
    assert result.valid is False
    assert set(result.details["field_errors"]) == {"sponsorship", "consent"}


def test_disabled_required_field_is_inactive_not_missing_answer():
    form = ApplicationForm("form-1", fields=[ApplicationField("conditional", "Conditional", required=True, disabled=True)])
    result = validate_application_form(form)
    assert result.valid is True
    assert result.details["field_results"]["conditional"]["code"] == "INACTIVE_FIELD"
    assert evaluate_safety_gate(_ready_context(form)).decision == ApplicationState.READY_TO_APPLY


def test_checkbox_multi_requires_selected_known_options():
    empty = ApplicationField("skills", "Skills", field_type="checkbox", semantic_type="checkbox_multi", multiple=True, required=True, options=["Python", "SQL"], value=[])
    assert validate_application_field(empty).valid is False
    selected = ApplicationField("skills", "Skills", field_type="checkbox", semantic_type="checkbox_multi", multiple=True, required=True, options=["Python", "SQL"], value=["Python"])
    assert validate_application_field(selected).valid is True
    unknown = ApplicationField("skills", "Skills", field_type="checkbox", semantic_type="checkbox_multi", multiple=True, options=["Python", "SQL"], value=["Rust"])
    assert validate_application_field(unknown).valid is False


def test_sponsorship_answer_uses_exact_available_option():
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = load_preferences(ROOT / "profile/preferences.yaml", {"requires_sponsorship": "yes"})
    field = ApplicationField(
        "sponsorship",
        "Will you require sponsorship?",
        field_type="radio",
        semantic_type="requires_sponsorship",
        options=["Yes, I will require sponsorship", "No, I will not require sponsorship"],
        confidence=1.0,
    )
    answer = AnswerKnowledgeBase([]).resolve_field(field, profile, preferences)
    assert answer is not None
    assert answer.answer == "Yes, I will require sponsorship"


def test_safety_gate_rejects_invalid_option_before_execution():
    context = _ready_context(ApplicationForm("form-1", fields=[ApplicationField("auth", "Authorization", field_type="select", required=True, options=["Yes", "No"], value="Brazil")]))
    readiness = evaluate_safety_gate(context)
    assert readiness.decision == ApplicationState.NEEDS_ANSWER
    assert "invalid_option:auth" in readiness.blockers
    with pytest.raises(ExecutionPlanError):
        build_execution_plan(context, _bindings_for(context.form))


def test_dry_run_and_live_plan_contracts_keep_submit_out_of_actions():
    dry = DryRunExecutionPlan("application-1", "greenhouse")
    assert dry.final_action == "STOP_BEFORE_SUBMIT"
    live = LiveApplicationPlan("application-1", "greenhouse", [ExecutionAction("advance", "step-1")])
    assert validate_live_application_plan(live).valid is True
    blocked = LiveApplicationPlan("application-1", "greenhouse", [ExecutionAction("submit", "")])
    assert validate_live_application_plan(blocked).valid is False


def test_execution_plan_and_dry_run_have_no_submit_path(tmp_path: Path):
    artifact = tmp_path / "resume.pdf"
    artifact.write_bytes(_valid_pdf())
    form = ApplicationForm(
        "greenhouse-1",
        provider="greenhouse",
        fields=[
            ApplicationField("name", "Name", required=True, value="Candidate"),
            ApplicationField("resume", "Resume", field_type="File", semantic_type="resume", required=True, attachment_path=str(artifact), accepted_types=["pdf"]),
        ],
        artifact_root=str(tmp_path),
    )
    context = _ready_context(form)
    bindings = _bindings_for(form)
    plan = build_execution_plan(context, bindings)
    assert plan.final_action == "STOP_BEFORE_SUBMIT"
    assert [action.action_type for action in plan.actions] == ["fill", "upload"]
    executor = DryRunBrowserExecutor()
    result = executor.execute(context, plan, bindings)
    assert result.stopped_before_submit is True
    assert [item["operation"] for item in result.operations] == ["fill", "upload"]
    assert not hasattr(executor, "submit")


def test_execution_plan_detects_changed_upload(tmp_path: Path):
    artifact = tmp_path / "resume.pdf"
    artifact.write_bytes(_valid_pdf())
    form = ApplicationForm("form-1", provider="greenhouse", artifact_root=str(tmp_path), fields=[ApplicationField("resume", "Resume", field_type="file", required=True, attachment_path=str(artifact), accepted_types=["pdf"])])
    context = _ready_context(form)
    bindings = _bindings_for(form)
    plan = build_execution_plan(context, bindings)
    artifact.write_bytes(artifact.read_bytes() + b"changed")
    with pytest.raises(BrowserSessionError, match="hash"):
        DryRunBrowserExecutor().execute(context, plan, bindings)


def test_executor_rechecks_safety_gate_and_current_form():
    context = _ready_context(ApplicationForm("form-1", provider="greenhouse", fields=[ApplicationField("name", "Name", required=True, value="Candidate")]))
    bypassed = ExecutionPlan("application-1", "greenhouse", [ExecutionAction("fill", "name", "invented")])
    with pytest.raises(BrowserSessionError, match="refusing invalid execution plan"):
        DryRunBrowserExecutor().execute(context, bypassed, _bindings_for(context.form))
    unknown = ExecutionPlan("application-1", "greenhouse", [ExecutionAction("fill", "other", "Candidate")])
    with pytest.raises(BrowserSessionError, match="refusing invalid execution plan"):
        DryRunBrowserExecutor().execute(context, unknown, _bindings_for(context.form))


def test_navigation_policy_rejects_local_and_non_web_urls():
    assert validate_navigation_url("file:///etc/passwd").valid is False
    assert validate_navigation_url("http://127.0.0.1:8080").valid is False
    assert validate_navigation_url("http://169.254.169.254/latest/meta-data").valid is False
    assert validate_navigation_url("https://example.com:8443").valid is False
    assert validate_navigation_url("https://8.8.8.8", {"example.com"}).valid is False
    assert validate_navigation_url("https://8.8.8.8").valid is True
    with pytest.raises(BrowserSessionError, match="allowed_hosts"):
        PlaywrightSessionManager().start()


def test_network_guard_records_aggregate_evidence_without_query_strings():
    class Request:
        method = "POST"
        resource_type = "fetch"
        url = "https://example.com/apply?email=candidate@example.com"

    guard = NetworkWriteGuard({"example.com"})
    assert guard.inspect(Request()) is False
    assert guard.blocked_writes[0].origin == "https://example.com"
    assert len(guard.blocked_writes[0].path_hash) == 16
    assert "candidate@example.com" not in str(guard.blocked_writes[0].__dict__)


def test_network_guard_tracks_pending_reads():
    class Request:
        method = "GET"
        resource_type = "fetch"
        url = "https://example.com/options"

    guard = NetworkWriteGuard({"example.com"})
    request = Request()
    assert guard.pending_read_count == 0
    guard.begin_read(request)
    assert guard.pending_read_count == 1
    guard.finish_read(request)
    assert guard.pending_read_count == 0


def test_inspector_separates_domain_form_from_dom_bindings():
    html = """
    <form data-provider="greenhouse">
      <label for="name">Full name</label><input id="name" name="name" required>
      <label>Consent <input id="consent" type="checkbox" required></label>
      <label><input name="skills" type="checkbox" value="Python">Python</label>
      <label><input name="skills" type="checkbox" value="SQL">SQL</label>
      <label for="resume">Resume</label><input id="resume" type="file" accept="application/pdf">
      <label for="country">Country</label><select id="country" name="country"><option value="br">Brazil</option><option value="us">United States</option></select>
    </form>
    """
    inspected = ATSInspector().inspect_html(html, "https://boards.greenhouse.io/example")
    assert inspected.form.provider == "greenhouse"
    by_key = {field.key: field for field in inspected.form.fields}
    assert by_key["consent"].semantic_type == "checkbox_boolean"
    assert by_key["skills"].semantic_type == "checkbox_multi"
    assert by_key["skills"].options == ["Python", "SQL"]
    assert by_key["resume"].field_type == "file"
    assert by_key["resume"].accepted_types == ["application/pdf"]
    assert inspected.bindings.for_field("country").locator == "#country"
    assert inspected.bindings.for_field("country").option_locators["Brazil"] == '#country option[value="br"]'
    assert not hasattr(inspected.form.fields[0], "locator")


def test_radio_option_bindings_are_unique_without_ids():
    inspected = ATSInspector().inspect_html("""
      <label>Yes <input name="authorization" type="radio" value="yes"></label>
      <label>No <input name="authorization" type="radio" value="no"></label>
    """)
    binding = inspected.bindings.for_field("authorization")
    assert binding.option_locators["Yes"] != binding.option_locators["No"]
    assert binding.option_values == {"Yes": "yes", "No": "no"}


def test_inspector_rejects_ambiguous_forms_and_validates_current_dom():
    html = """
      <form id="search"><input name="q"></form>
      <form id="application"><label for="email">Email</label><input id="email" name="email" type="email"></form>
    """
    with pytest.raises(InspectionError, match="AMBIGUOUS_FORM"):
        ATSInspector().inspect_html(html)
    inspected = ATSInspector().inspect_html(html, form_selector="#application")
    assert validate_bindings_against_html(inspected.form, inspected.bindings, html).valid is True
    changed = html.replace('id="email"', 'id="email-changed"')
    assert validate_bindings_against_html(inspected.form, inspected.bindings, changed).valid is False


class _FakeLocator:
    def __init__(self, calls: list[tuple[str, str]], selector: str):
        self.calls = calls
        self.selector = selector

    def count(self):
        return 1

    def fill(self, value):
        self.calls.append(("fill", f"{self.selector}:{value}"))

    def set_input_files(self, path):
        self.calls.append(("upload", path))


class _FakePage:
    def __init__(self, html: str):
        self.html = html
        self.calls: list[tuple[str, str]] = []

    def content(self):
        return self.html

    def evaluate(self, script, arg=None):
        return True

    def wait_for_timeout(self, milliseconds):
        return None

    def locator(self, selector):
        return _FakeLocator(self.calls, selector)


class _FakeGuardedSession(PlaywrightSessionManager):
    def __init__(self, page):
        self.headless = True
        self.page = page
        self.context = object()
        self.network_guard = NetworkWriteGuard({"example.com"})
        self.guarded = True


def test_playwright_filler_only_fills_and_uploads(tmp_path: Path):
    artifact = tmp_path / "resume.pdf"
    artifact.write_bytes(_valid_pdf())
    html = '<form id="application"><label for="name">Name</label><input id="name" required><label for="resume">Resume</label><input id="resume" type="file" accept="application/pdf" required></form>'
    inspected = ATSInspector().inspect_html(html, form_selector="#application")
    inspected.form.artifact_root = str(tmp_path)
    for field in inspected.form.fields:
        if field.key == "name":
            field.value = "Candidate"
        if field.key == "resume":
            field.attachment_path = str(artifact)
    context = _ready_context(inspected.form)
    plan = build_execution_plan(context, inspected.bindings)
    page = _FakePage(html)
    session = _FakeGuardedSession(page)
    result = PlaywrightFormFiller().fill(session, context, plan, inspected.bindings)
    assert [call[0] for call in page.calls] == ["fill", "upload"]
    assert result.stopped_before_submit is True
    assert not hasattr(PlaywrightFormFiller(), "submit")


def test_playwright_filler_rejects_raw_page_without_guarded_session():
    html = '<form id="application"><input id="name" name="name" required></form>'
    inspected = ATSInspector().inspect_html(html, form_selector="#application")
    inspected.form.fields[0].value = "Candidate"
    context = _ready_context(inspected.form)
    plan = build_execution_plan(context, inspected.bindings)
    with pytest.raises(BrowserSessionError, match="GuardedBrowserSession"):
        PlaywrightFormFiller().fill(_FakePage(html), context, plan, inspected.bindings)


def test_playwright_filler_stops_when_dom_changes_after_action():
    html = '<form id="application"><input id="first" name="first" required><input id="second" name="second" required></form>'
    inspected = ATSInspector().inspect_html(html, form_selector="#application")
    for field in inspected.form.fields:
        field.value = "Candidate"
    context = _ready_context(inspected.form)
    plan = build_execution_plan(context, inspected.bindings)

    class ChangingPage(_FakePage):
        def evaluate(self, script, arg=None):
            if "MutationObserver" in script:
                self.html = self.html.replace("</form>", '<input id="conditional" name="conditional" required></form>')
                return True
            return super().evaluate(script, arg)

    page = ChangingPage(html)
    result = PlaywrightFormFiller().fill(_FakeGuardedSession(page), context, plan, inspected.bindings)
    assert result.status == "FORM_CHANGED"
    assert len(result.operations) == 1


# --- escolha de opcao em combobox (funcao pura, sem browser) ------------------


def test_affirm_intent_maps_to_the_real_ats_label():
    """A resposta e "I agree"; as opcoes do ATS sao Yes/Acknowledge."""
    from jobsearch_agent.qa import AFFIRM_SOURCE

    assert choose_option_index(["No", "Yes"], "i agree", AFFIRM_SOURCE) == 1
    assert choose_option_index(["Acknowledge/Confirm", "Decline"], "i agree", AFFIRM_SOURCE) == 0
    assert choose_option_index(["No", "I accept"], "i agree", AFFIRM_SOURCE) == 1
    assert choose_option_index(["Cancel", "I consent"], "i agree", AFFIRM_SOURCE) == 1


def test_decline_intent_maps_to_every_real_decline_label():
    from jobsearch_agent.qa import DECLINE_SOURCE

    assert choose_option_index(["Agender", "Male", "I don't wish to answer"], "decline to self-identify", DECLINE_SOURCE) == 2
    assert choose_option_index(["Yes", "No", "Decline To Self Identify"], "decline to self-identify", DECLINE_SOURCE) == 2
    assert choose_option_index(["Yes, I have a disability", "No, I do not", "I do not want to answer"], "decline to self-identify", DECLINE_SOURCE) == 2


def test_decline_never_matches_the_opposite_statement():
    """Marcador parcial casaria com "I wish to answer", que e o oposto."""
    from jobsearch_agent.qa import DECLINE_SOURCE

    with pytest.raises(OptionSelectionError) as exc:
        choose_option_index(["I wish to answer", "I want to answer"], "decline to self-identify", DECLINE_SOURCE)
    assert exc.value.code == "OPTION_NOT_FOUND_COMBOBOX_OPTION"


def test_ambiguous_intent_match_is_refused_not_guessed():
    from jobsearch_agent.qa import AFFIRM_SOURCE, DECLINE_SOURCE

    with pytest.raises(OptionSelectionError) as affirm:
        choose_option_index(["Yes", "I agree"], "i agree", AFFIRM_SOURCE)
    assert affirm.value.code == "AMBIGUOUS_COMBOBOX_OPTION"

    with pytest.raises(OptionSelectionError) as decline:
        choose_option_index(["Decline to self-identify", "I don't wish to answer"], "x", DECLINE_SOURCE)
    assert decline.value.code == "AMBIGUOUS_COMBOBOX_OPTION"


def test_plain_value_matches_exactly_then_by_prefix():
    assert choose_option_index(["Brazil", "Canada"], "brazil") == 0
    # O seletor de pais do telefone decora o rotulo com o DDI.
    assert choose_option_index(["United States +1", "Brazil +55"], "brazil") == 1
    # Prefixo ambiguo nao e escolhido.
    with pytest.raises(OptionSelectionError) as exc:
        choose_option_index(["Brazil +55", "Brazil +554"], "brazil")
    assert exc.value.code == "AMBIGUOUS_COMBOBOX_OPTION"


def test_missing_option_is_reported_with_the_documented_code():
    with pytest.raises(OptionSelectionError) as exc:
        choose_option_index(["Yes", "No"], "maybe")
    assert exc.value.code == "OPTION_NOT_FOUND_COMBOBOX_OPTION"


def test_marker_comparison_ignores_apostrophes_and_case():
    from jobsearch_agent.qa import DECLINE_SOURCE

    assert choose_option_index(["I DON'T WISH TO ANSWER"], "x", DECLINE_SOURCE) == 0
    assert choose_option_index(["i do not want to answer"], "x", DECLINE_SOURCE) == 0


# --- escrita autorizada one-shot ----------------------------------------------


class _Request:
    def __init__(self, method: str, url: str, resource_type: str = "fetch") -> None:
        self.method = method
        self.url = url
        self.resource_type = resource_type


SUBMIT_URL = "https://boards.greenhouse.io/canonical/jobs/5150422"


def _permit(**overrides):
    from jobsearch_agent.browser import AuthorizedWrite

    values = {
        "application_id": "application-1",
        "submission_intent_id": "intent-1",
        "origin": "https://boards.greenhouse.io",
        "path_pattern": r"^/[^/]+/jobs/[^/]+/?$",
        "method": "POST",
        "max_writes": 1,
    }
    values.update(overrides)
    return AuthorizedWrite(**values)


def test_write_is_blocked_while_no_permit_is_armed():
    guard = NetworkWriteGuard({"boards.greenhouse.io"})
    assert guard.inspect(_Request("POST", SUBMIT_URL)) is False
    assert guard.blocked_writes
    assert guard.authorized_writes_remaining == 0


def test_armed_permit_allows_exactly_one_matching_write():
    guard = NetworkWriteGuard({"boards.greenhouse.io"})
    guard.arm_write(_permit())
    assert guard.authorized_writes_remaining == 1

    assert guard.inspect(_Request("POST", SUBMIT_URL)) is True
    assert guard.authorized_writes_remaining == 0

    # A segunda escrita nao esta mais coberta.
    assert guard.inspect(_Request("POST", SUBMIT_URL)) is False
    assert len(guard.blocked_writes) == 1


def test_permit_does_not_cover_a_different_origin_path_or_method():
    guard = NetworkWriteGuard({"boards.greenhouse.io"})
    guard.arm_write(_permit())

    assert guard.inspect(_Request("POST", "https://evil.example/canonical/jobs/1")) is False
    assert guard.inspect(_Request("POST", "https://boards.greenhouse.io/other/endpoint")) is False
    assert guard.inspect(_Request("PUT", SUBMIT_URL)) is False
    assert guard.inspect(_Request("DELETE", SUBMIT_URL)) is False
    # A permissao segue intacta: nenhuma delas a consumiu.
    assert guard.authorized_writes_remaining == 1
    assert guard.inspect(_Request("POST", SUBMIT_URL)) is True


def test_reads_stay_allowed_with_a_permit_armed_and_are_not_consumed():
    guard = NetworkWriteGuard({"boards.greenhouse.io"})
    guard.arm_write(_permit())
    assert guard.inspect(_Request("GET", SUBMIT_URL)) is True
    assert guard.inspect(_Request("OPTIONS", SUBMIT_URL)) is True
    assert guard.authorized_writes_remaining == 1


def test_disarming_restores_the_full_block():
    guard = NetworkWriteGuard({"boards.greenhouse.io"})
    guard.arm_write(_permit())
    guard.disarm_write()
    assert guard.inspect(_Request("POST", SUBMIT_URL)) is False
    assert guard.authorized_write is None


def test_permit_rejects_a_non_positive_write_budget():
    guard = NetworkWriteGuard({"boards.greenhouse.io"})
    with pytest.raises(ValueError, match="at least one request"):
        guard.arm_write(_permit(max_writes=0))


def test_websocket_is_never_authorized_even_with_a_permit():
    guard = NetworkWriteGuard({"boards.greenhouse.io"})
    guard.arm_write(_permit())
    assert guard.inspect(_Request("GET", SUBMIT_URL, resource_type="websocket")) is False


def test_option_matching_ignores_punctuation_on_either_side():
    """Regressao: o rotulo real tem parenteses que a resposta nao tem."""
    assert choose_option_index(["Brazilian Grading System (0-19)", "GPA (4.0 Scale)"], "brazilian grading system (0-19)") == 0
    assert choose_option_index(["Brazilian Grading System (0-19)", "GPA (4.0 Scale)"], "Brazilian Grading System 0 19") == 0
    assert choose_option_index(["Acknowledge/Confirm"], "acknowledge confirm") == 0
    # Prefixo continua valendo apos normalizar.
    assert choose_option_index(["Brazil +55", "Canada +1"], "brazil") == 0


def test_two_scoped_permits_have_independent_budgets():
    """Subida do curriculo (S3) e POST de submissao sao escritas distintas."""
    guard = NetworkWriteGuard({"boards.greenhouse.io"})
    guard.arm_writes([
        _permit(origin="*.s3.amazonaws.com", path_pattern=r"^/.*$"),
        _permit(origin="https://boards.greenhouse.io", path_pattern=r"^/[^/]+/jobs/[^/]+/?$"),
    ])
    upload = _Request("POST", "https://grnhse-prod-jben-us-east-1.s3.amazonaws.com/upload")
    assert guard.inspect(upload) is True
    assert guard.inspect(_Request("POST", SUBMIT_URL)) is True
    # Cada permissao esgota sozinha e nenhuma libera a outra.
    assert guard.inspect(_Request("POST", SUBMIT_URL)) is False
    assert guard.inspect(upload) is False
    assert guard.inspect(_Request("POST", "https://evil.example/upload")) is False
    assert guard.authorized_writes_used == 2


def test_write_usage_survives_disarming_for_audit():
    guard = NetworkWriteGuard({"boards.greenhouse.io"})
    guard.arm_write(_permit())
    guard.inspect(_Request("POST", SUBMIT_URL))
    guard.disarm_write()
    # As permissoes foram revogadas, mas o registro do que foi autorizado fica.
    assert guard.authorized_write is None
    assert guard.authorized_writes_remaining == 0
    assert guard.authorized_writes_used == 1
