# ADR 0008: the multi-step application contract

- **Status:** Accepted
- **Date:** 2026-09-24

## Context

The live orchestrator already had a multi-step loop: inspect → resolve → fill → click `Next` →
inspect again, with a fingerprint per screen and a bound on cycles. What it did *not* have was a
correct notion of **the application**.

Each cycle did `context.form = inspected.form` and `context.answers = resolved`, and at the end the
authorization was built from `live.form`:

```text
form = live.form
answers_fingerprint = compute_answers_fingerprint(form)
ReviewSnapshot(form)          SubmissionIntent(answers_fingerprint)
```

On a real multi-step form the DOM of step 2 replaces step 1. So at submit time `live.form` could
contain only the last screen, and the system would have:

```text
browser filled four screens
answers_fingerprint   covering one screen
ReviewSnapshot        covering one screen
SubmissionIntent      not bound to the answers it was said to authorize
```

Nothing errors in that state. The submission succeeds and the audit is wrong — which is worse, because
"authorized" becomes a claim nobody can check.

## Decision

### `ApplicationForm` stays "the DOM now"; the contract is a separate object

`ApplicationJourney` (`journey.py`) accumulates `ApplicationStep`s. Each step records the form
fingerprint, the URL, the provider, the field keys and the **per-field decision** for that screen.
`ApplicationForm` keeps meaning exactly one thing, and the accumulator carries the whole application.

### Two fingerprints, and they are not the same thing

```text
form_fingerprint      the FINAL surface where Submit is executed
                      (still what detects a form change right before the POST)

answers_fingerprint   EVERY approved answer, from EVERY step
```

`ApplicationJourney.answers_fingerprint()` builds a synthetic form from the accumulated fields and
reuses `compute_answers_fingerprint`. For a single-step application the value is byte-identical to
before, so nothing changes for the existing flow — and for a multi-step one it covers the contract
instead of the last screen.

### Identity is stable across steps, and collisions are not resolved silently

A field's identity is `provider|field_key` — deliberately **without** the step index. Including the
step would make the same field on two screens two identities, and a contradiction would pass
unnoticed. Same identity with the same value is idempotent; the same identity with a *different*
non-empty value is a `ContractConflict`:

```text
step 1: requires_sponsorship = No
step 4: requires_sponsorship = Yes
→ CONTRACT_CONFLICT, the first answer stays, nothing is submitted
```

The last screen never silently wins. The first decision recorded for a field is the one that stays in
the accumulated provenance — accumulating must not erase what was decided earlier.

### The browser submits the real form; the contract authorizes and audits

The accumulated contract does **not** reconstruct the POST. `SubmissionCoordinator` uses it to build
the `ReviewSnapshot` (all steps) while the browser submits the page's own form. The snapshot is the
durable audit artifact, and it must answer "what did the agent answer, where, and on whose authority".

### Step advancement stays local

`Next`/`Continue`/`Review` are clicked only when they are `button[type="button"]` with a known advance
label, and any write they attempt is blocked (`ADVANCE_BLOCKED_BY_NETWORK_POLICY`). This ADR proves
multi-step **without** intermediate writes; boards that save a step through an API need their own
boundary, which is JSA-LOOP-003 and must be declared per provider — never a generic `POST` during
`Next`.

### The review screen is additive, and that is deliberate

A `File` cannot be carried into a hidden input, so the screen holding the resume must still be in the
DOM when Submit happens. The controlled ATS models this: the review step appends a screen instead of
replacing it, which also changes the final fingerprint (a new field key) without adding a required
question.

## Consequences

- A multi-step application now ends in `SUBMITTED` with the server receiving **all** fields, exactly
  one POST, zero intermediate writes, and the approved PDF by SHA.
- The result reports the truth about the whole application: `cycles` (screens inspected),
  `steps_completed` (advances), `questions_answered` and `unanswered_required` from the accumulated
  contract. In the certified scenario: 5 screens, 4 advances, 13 answered.
- Three failure modes stop the flow with **zero** submissions: a repeated fingerprint
  (`LOOP_DETECTED`), exceeding `max_cycles` (`MAX_CYCLES_EXCEEDED`) and a factual question without a
  fact mid-flow (`NEEDS_ANSWER` with the journey showing exactly how far it got).
- `requires_action` may name an **action** (`LOOP_DETECTED`, `CONTRACT_CONFLICT`) rather than a state:
  what is missing there is not an answer, it is a fix.
- A mutation check confirms the fix is load-bearing: reverting the coordinator to the last screen makes
  the persisted snapshot lose `first_name`, `experience_years` and `why_this_role`, and the E2E fails.
