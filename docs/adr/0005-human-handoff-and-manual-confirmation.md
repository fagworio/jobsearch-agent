# ADR 0005: human handoff and manual submission confirmation

- **Status:** Accepted
- **Date:** 2026-09-24

## Context

ADR 0003 introduced `NEEDS_HUMAN_CAPTCHA` for a submission that was sent and refused by anti-bot
verification. That state has exactly one outgoing edge today — `REVIEW_REACHED`, driven by the
explicit `retry-submit` command — because there was nowhere else for it to go.

The Human Final Mile gives it somewhere else to go, and that opens a question the current domain
cannot answer: a person finishes the application manually in their own browser, and afterwards the
system holds a claim it cannot verify. Three things that look similar are in fact different, and the
domain must not collapse them:

```text
NEEDS_HUMAN_CAPTCHA  the provider refused; nothing usable was sent
SUBMIT_UNKNOWN       the agent sent it and cannot determine the outcome
SUBMITTED            independent evidence says it was accepted
```

A user saying "I submitted it" is none of those. It is a report, not evidence. Treating the report as
`SUBMITTED` would let a claim satisfy the very uncertainty it creates, which is precisely the failure
this project spent its invariant budget avoiding for automatic submission.

## Decision

### A report is an event; the resulting uncertainty is a state

```text
HANDOFF_IN_PROGRESS              state
MANUAL_SUBMISSION_REPORTED       event
AWAITING_SUBMISSION_CONFIRMATION state
```

`MANUAL_SUBMISSION_REPORTED` describes something that **happened**, not the system's current level of
certainty. The persistent state after it is `AWAITING_SUBMISSION_CONFIRMATION`, which says exactly
what is true: there is a claim of manual submission, and no independent evidence yet.

### State machine

```text
SUBMITTING
   │ anti-bot rejection
   ▼
NEEDS_HUMAN_CAPTCHA
   ├── retry-submit (explicit) ────────────→ REVIEW_REACHED
   │
   └── handoff ────────────────────────────→ HANDOFF_IN_PROGRESS
                                                 │
                                    MANUAL_SUBMISSION_REPORTED
                                                 ▼
                                    AWAITING_SUBMISSION_CONFIRMATION
                                                 │
                                       SUBMISSION_CONFIRMED
                                                 ▼
                                             SUBMITTED
```

`NEEDS_HUMAN_CAPTCHA → REVIEW_REACHED` is **kept**, but only for the explicit `retry-submit`
command. The manual path is additive, not a replacement.

### Commands and events are distinct

```text
command: START_HUMAN_HANDOFF        NEEDS_HUMAN_CAPTCHA -> HANDOFF_IN_PROGRESS
command: CANCEL_HANDOFF             HANDOFF_IN_PROGRESS  -> REVIEW_REACHED
event:   MANUAL_SUBMISSION_REPORTED HANDOFF_IN_PROGRESS  -> AWAITING_SUBMISSION_CONFIRMATION
event:   SUBMISSION_CONFIRMED       AWAITING_SUBMISSION_CONFIRMATION -> SUBMITTED
```

`CANCEL_HANDOFF` is safe because no submission has been claimed yet. After
`MANUAL_SUBMISSION_REPORTED` there is **no** cancellation edge back to `REVIEW_REACHED`: a real
possibility exists that the application was already sent, and reopening the submit path would break
exactly-once. Reconciling a claim that turns out to be wrong requires an explicit reconciliation
flow, not a retry.

### `AWAITING_SUBMISSION_CONFIRMATION` behaves like `SUBMIT_UNKNOWN`

Both mean "do not resend automatically". The difference is the **origin** of the uncertainty:

```text
SUBMIT_UNKNOWN                    the agent sent it and could not determine the outcome
AWAITING_SUBMISSION_CONFIRMATION  a human reports having sent it, without independent evidence
```

The state therefore has no edges to `REVIEW_REACHED`, `SUBMIT_AUTHORIZED`, `SUBMITTING` or any other
path that could produce another submission attempt.

### What may satisfy the confirmation edge

The edge must not depend on one specific source, and it must not be satisfiable by the report itself.
It is defined against a conceptual interface:

```text
SubmissionConfirmationEvidence        (JSA-CG-018, implemented)
    source:       confirmation_email
                | provider_confirmation_page
                | provider_application_status
                | externally_verified_record
    observed_at:  ISO-8601, explicit timezone, never in the future
    reference:    opaque identifier of the evidence, never its content
    provider:     who observed it (the ATS, the mail provider, the external record)
    confidence:   [0,1]
```

Gmail integration (JSA-CG-018's future work) is one implementation of `confirmation_email`. The rule is:

```text
AWAITING_SUBMISSION_CONFIRMATION + accepted independent evidence -> SUBMITTED
```

and never:

```text
user reported submission -> SUBMITTED
```

Four things are **declarations**, not evidence, and are refused by name:

```text
user_report            manual_checkbox        free_text        handoff_completion
```

They are listed as data (`REJECTED_EVIDENCE_KINDS`), not merely omitted from the accepted set, so the
refusal can be named in a test and in an error message. The source is a closed set, so a declaration
cannot even be *constructed* as evidence; the confidence floor (`0.5`) additionally refuses evidence
the observer itself places at the level of chance. Below the floor the evidence is still recorded and
refused — never silently discarded.

The rule lives in `assert_confirmation_evidence`, called from `ApplicationService.transition` whenever
the target is `SUBMITTED`, and not in `confirm_submission` — so a future command that tries to reach
`SUBMITTED` passes through the same check instead of re-implementing it.

Precisely: **`transition()` is the only door of the manual/domain flow to `SUBMITTED`.** The
automatic executor has its own transactional path — `Database.complete_submission_attempt` moves
`SUBMITTING → SUBMITTED` inside the single transaction that closes the attempt, with the provider's
own response as the evidence. That is not a bypass: it is a different evidence regime (the provider's
response to a write this system authorized and observed), and it was the first one to exist. An audit
reading direct SQL state updates as a violation of this ADR would be reading the wrong rule; what the
invariant forbids is a *declaration* satisfying the edge, not the executor's own confirmation.

There is deliberately **no CLI** for confirmation yet. A command that takes a source and a reference
from the command line *is* the free-text path this ADR refuses; the operation becomes reachable from
outside once a real observer exists (provider status, confirmation e-mail).

### Invariants, as testable rules

> **1. No command based only on a user declaration may transition an Application to `SUBMITTED`.**

> **2. An Application in `AWAITING_SUBMISSION_CONFIRMATION` may not start a new submission attempt.**

Both are stated as domain rules rather than conventions because they are the two most dangerous bugs
this evolution can produce, and both are mechanically checkable against the transition map.

## Consequences

- JSA-CG-015 becomes small: `application handoff <application-id>` validates `NEEDS_HUMAN_CAPTCHA`,
  consumes `ChallengeOutcome.handoff` (already produced by the challenge-guard integration),
  enriches it with job, resume and approved answers, persists a `HANDOFF_STARTED` event and moves the
  application to `HANDOFF_IN_PROGRESS`.
- JSA-CG-017 adds `application report-manual-submit <application-id>`, which emits the event and moves
  to `AWAITING_SUBMISSION_CONFIRMATION`. It explicitly does **not** mark `SUBMITTED`.
- JSA-CG-018 supplies the evidence that makes the final edge possible. Until it exists, that edge does
  not exist either — an edge without evidence would be the same conflation this ADR rejects.
- `NEEDS_HUMAN_CAPTCHA` gains a second outgoing edge, so the existing `retry-submit` path needs a
  test that it still requires the explicit command and cannot be reached from the handoff states.
- Two new states enter `ApplicationState`, which means every place that enumerates it must be
  revisited. Levantados agora, para a implementação não descobrir um por vez:

  ```text
  application.py  TRANSITIONS          duas entradas novas + as arestas do diagrama
  application.py  retry_submit         `reopenable` NAO pode ganhar os estados de handoff
  application.py  resume               decidir se HANDOFF_IN_PROGRESS e retomavel
  pipeline.py     apply_live           o conjunto que autoriza dry-run nao deve crescer
  ```

  Foi exatamente esta classe de mudança que produziu defeitos quando `NEEDS_HUMAN_CAPTCHA` entrou.

## Addendum: JSA-CG-016 is implemented **before** JSA-CG-015

The implementation order was inverted relative to the first draft of the consequences above, and the
reason is atomicity. It must not be possible for

```text
NEEDS_HUMAN_CAPTCHA -> HANDOFF_IN_PROGRESS
```

to happen and only then be discovered that the material a human needs in order to continue could not
be produced. That would create an orphan state: the application says "a person is finishing this",
and no person has anything to finish it with.

### The contract

```text
build package -> validate package -> persist package -> record handoff_started -> HANDOFF_IN_PROGRESS
```

If any earlier step fails, the Application stays in `NEEDS_HUMAN_CAPTCHA`. The package row, the state
update and the `handoff_started` event are written in **one** SQLite transaction
(`Database.save_handoff_package`); the bundle on disk is written *before* it and removed again if the
transaction fails. The only reachable half-state is a bundle with the application still in
`NEEDS_HUMAN_CAPTCHA`, which is harmless and is overwritten by the next attempt.

### `HumanHandoffPackage` is domain, not challenge-guard

`challenge_guard.HumanHandoff` is deliberately neutral: provider, reason, session, page, continuation.
It knows nothing about a job, a resume or answers, and it must not learn. The domain adds only what it
already knows and what the human needs:

```text
package_id                endereço derivado do conteúdo
application_id, job_id
provider, reason_token, challenge_session_id    vindos do HumanHandoff
destination               endpoint revisado da candidatura
page_url                  página que o humano abre
company, title
resume_path, resume_sha256
answers_fingerprint, approved_answers
continuation, instructions
package_path, package_sha256, created_at
```

### Immutability

The bundle lives in the private artifacts directory as `package.json` plus a **copy** of the resume,
under `handoff/<package_id>/`. The `package_id` is derived from the approved material and from the
recorded handoff event, so identical material is the same package and different material is a
different one; a package is never rewritten. `package_sha256` covers the whole content, and
`from_dict` refuses a bundle whose content no longer matches its digest. If the resume or the answers
change later, the old package stays exactly as approved and remains auditable.

`approved_answers` carries only answers with `approved = True`. The persisted form is cross-checked
against the `ReviewSnapshot` fingerprint, so a form edited after review cannot be handed off.

### The event journal carries a reference, not content

```json
{
  "previous_state": "NEEDS_HUMAN_CAPTCHA",
  "handoff_package_id": "hpkg-…",
  "reason_token": "…",
  "provider": "…",
  "challenge_session_id": "…"
}
```

`ApplicationService.start_human_handoff` enforces that allowlist with a short-token check: a value that
is not an opaque token is refused instead of being written to the journal. Answers, resume contents,
names and page URLs with queries never enter an event.

### Reconstructing the handoff without the live process

The process that observed the challenge is gone by the time a person asks for the package. The attempt
therefore records the **observed** provenance — challenge provider, reason token from the library's
closed set, and session id — and `application handoff` rebuilds the neutral handoff from it through
the ACL. Without recorded provenance there is no reconstruction: inventing "hcaptcha" from the state
would assert a fact nobody observed, so the command fails closed and says so.

### Consequences for JSA-CG-015

The CLI is a thin shell over `HumanHandoffService.prepare_handoff`: it prints the package's safe view
(no answers, no absolute paths) and never decides anything itself. Re-running it while the application
is in `HANDOFF_IN_PROGRESS` re-reads the recorded package instead of creating a second one.

### JSA-CG-017 and JSA-CG-018, as implemented

```text
application report-manual-submit <application-id>      HANDOFF_IN_PROGRESS -> AWAITING_SUBMISSION_CONFIRMATION
ApplicationService.confirm_submission(id, evidence)    AWAITING_... -> SUBMITTED   (evidence required)
```

The report event carries the handoff package reference (`handoff_package_id`) and nothing else: it is
what ties the declaration to the exact material the human says they used. The package, the resume copy
and the answers fingerprint are **not** rewritten by the report — verified on real material — so the
bundle that supported the claim stays auditable exactly as it was when the automation stopped.

Three guarantees are tested, not assumed:

```text
report-manual-submit                     -> never SUBMITTED
AWAITING_SUBMISSION_CONFIRMATION         -> no retry-submit, no new submission attempt
MANUAL_SUBMISSION_REPORTED               -> package SHA, resume SHA and answers fingerprint unchanged
```

The real run that this design came from, including the corrected framing of the provider attribution,
is recorded in [the CI&T evidence note](../evidence/2026-09-24-lever-ciandt-antibot-refusal.md).


