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
SubmissionConfirmationEvidence
    source: confirmation_email
          | provider_confirmation_page
          | provider_application_status
          | externally_verified_record
    reference: <opaque identifier of the evidence, never its content>
```

Gmail integration (JSA-CG-018) is one future implementation of `confirmation_email`. The rule is:

```text
AWAITING_SUBMISSION_CONFIRMATION + accepted independent evidence -> SUBMITTED
```

and never:

```text
user reported submission -> SUBMITTED
```

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
