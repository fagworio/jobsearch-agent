from dataclasses import dataclass

from jobsearch_agent.ats import GreenhouseAdapter
from jobsearch_agent.browser import BrowserExecutionResult
from jobsearch_agent.models import ApplicationAnswer, ApplicationContext, ApplicationPolicy, CandidatePreferences
from jobsearch_agent.orchestrator import DryRunApplicationOrchestrator
from jobsearch_agent.profile import load_profile
from jobsearch_agent.qa import AnswerKnowledgeBase


PROFILE = load_profile("profile/career_profile.yaml")
PREFERENCES = CandidatePreferences(relocation=True, work_authorization=["Brazil"])


INITIAL_HTML = """
<form id="application_form">
  <label for="email">Email</label>
  <input id="email" name="job_application[email]" required>
</form>
"""

CHANGED_HTML = """
<form id="application_form">
  <label for="email">Email</label>
  <input id="email" name="job_application[email]" required>
  <label for="relocation">Are you willing to relocate?</label>
  <select id="relocation" name="job_application[relocation]" required>
    <option value="">Choose</option><option value="yes">Yes</option><option value="no">No</option>
  </select>
</form>
"""

MANUAL_INITIAL_HTML = """
<form id="application_form">
  <label for="email">Email</label>
  <input id="email" name="job_application[email]" required>
  <label for="custom">Why are you interested?</label>
  <input id="custom" name="job_application[question_123]" required>
</form>
"""

MANUAL_CHANGED_HTML = MANUAL_INITIAL_HTML.replace(
    "</form>",
    '<label for="relocation">Are you willing to relocate?</label>'
    '<select id="relocation" name="job_application[relocation]" required>'
    '<option value="">Choose</option><option value="yes">Yes</option><option value="no">No</option>'
    "</select></form>",
)


@dataclass
class FakePage:
    html: str
    url: str = "https://boards.greenhouse.io/acme/jobs/1"

    def content(self) -> str:
        return self.html


@dataclass
class FakeSession:
    page: FakePage


class ChangingFiller:
    def __init__(self, page: FakePage):
        self.page = page
        self.calls = 0

    def fill(self, session, context, plan, bindings, audit_dir=None):
        self.calls += 1
        if self.calls == 1:
            self.page.html = CHANGED_HTML
            return BrowserExecutionResult(context.application_id, [{"operation": "fill", "field_key": "job_application[email]"}], status="FORM_CHANGED")
        return BrowserExecutionResult(context.application_id, [{"operation": "fill", "field_key": action.field_key} for action in plan.actions])


class AlwaysChangedFiller:
    def fill(self, session, context, plan, bindings, audit_dir=None):
        return BrowserExecutionResult(context.application_id, [], status="FORM_CHANGED")


class ManualAnswerChangingFiller:
    def __init__(self, page: FakePage):
        self.page = page
        self.calls = 0

    def fill(self, session, context, plan, bindings, audit_dir=None):
        self.calls += 1
        if self.calls == 1:
            self.page.html = MANUAL_CHANGED_HTML
            return BrowserExecutionResult(context.application_id, [], status="FORM_CHANGED")
        return BrowserExecutionResult(context.application_id, [{"field_key": action.field_key} for action in plan.actions])


def _context():
    return ApplicationContext(
        application_id="application-orchestrated",
        job_id="job-orchestrated",
        fit={"blockers": []},
        validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "manual"}),
    )


def test_orchestrator_reinspects_resolves_and_replans_after_form_change():
    page = FakePage(INITIAL_HTML)
    filler = ChangingFiller(page)
    result = DryRunApplicationOrchestrator(
        GreenhouseAdapter(), PROFILE, PREFERENCES, AnswerKnowledgeBase([]), filler=filler, max_cycles=3
    ).run(FakeSession(page), _context())

    assert result.status == "COMPLETED"
    assert filler.calls == 2
    assert [cycle.result for cycle in result.cycles] == ["FORM_CHANGED", "COMPLETED"]
    assert len(result.cycles) == 2
    assert result.plan is not None
    assert {action.field_key for action in result.plan.actions} == {
        "job_application[email]",
        "job_application[relocation]",
    }


def test_orchestrator_detects_repeated_fingerprint_before_second_execution():
    page = FakePage(INITIAL_HTML)
    filler = AlwaysChangedFiller()
    result = DryRunApplicationOrchestrator(
        GreenhouseAdapter(), PROFILE, PREFERENCES, AnswerKnowledgeBase([]), filler=filler, max_cycles=3
    ).run(FakeSession(page), _context())

    assert result.status == "LOOP_DETECTED"
    assert filler is not None
    assert len(result.cycles) == 1


def test_orchestrator_stops_at_max_cycles():
    page = FakePage(INITIAL_HTML)
    result = DryRunApplicationOrchestrator(
        GreenhouseAdapter(), PROFILE, PREFERENCES, AnswerKnowledgeBase([]), filler=AlwaysChangedFiller(), max_cycles=1
    ).run(FakeSession(page), _context())

    assert result.status == "MAX_CYCLES_EXCEEDED"
    assert len(result.cycles) == 1


def test_orchestrator_carries_only_approved_answer_across_reinspection():
    page = FakePage(MANUAL_INITIAL_HTML)
    context = _context()
    context.answers = [
        ApplicationAnswer(
            "manual-interest",
            "Why are you interested?",
            "The role matches my documented experience.",
            source="manual_review",
            approved=True,
            semantic_type="unknown",
            field_key="job_application[question_123]",
        )
    ]
    filler = ManualAnswerChangingFiller(page)
    result = DryRunApplicationOrchestrator(
        GreenhouseAdapter(), PROFILE, PREFERENCES, AnswerKnowledgeBase([]), filler=filler, max_cycles=3
    ).run(FakeSession(page), context)

    assert result.status == "COMPLETED"
    assert result.plan is not None
    manual_action = next(action for action in result.plan.actions if action.field_key == "job_application[question_123]")
    assert manual_action.value == "The role matches my documented experience."


def test_orchestrator_does_not_carry_unapproved_answer():
    page = FakePage(MANUAL_INITIAL_HTML)
    context = _context()
    context.answers = [
        ApplicationAnswer(
            "manual-interest",
            "Why are you interested?",
            "Unapproved answer",
            source="manual_review",
            approved=False,
            semantic_type="unknown",
        )
    ]
    result = DryRunApplicationOrchestrator(
        GreenhouseAdapter(), PROFILE, PREFERENCES, AnswerKnowledgeBase([]), filler=AlwaysChangedFiller(), max_cycles=3
    ).run(FakeSession(page), context)

    assert result.status == "NEEDS_ANSWER"


def test_orchestrator_fails_closed_for_ambiguous_global_approved_answers():
    page = FakePage(MANUAL_INITIAL_HTML)
    context = _context()
    context.answers = [
        ApplicationAnswer("one", "Why are you interested?", "Answer one", approved=True, semantic_type="unknown"),
        ApplicationAnswer("two", "Why are you interested?", "Answer two", approved=True, semantic_type="unknown"),
    ]
    result = DryRunApplicationOrchestrator(
        GreenhouseAdapter(), PROFILE, PREFERENCES, AnswerKnowledgeBase([]), filler=AlwaysChangedFiller(), max_cycles=3
    ).run(FakeSession(page), context)

    assert result.status == "NEEDS_ANSWER"
