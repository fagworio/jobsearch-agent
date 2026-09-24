# ADR 0006: observable confirmation

- **Status:** Accepted
- **Date:** 2026-09-24

## Context

ADR 0005 made `SUBMITTED` require `SubmissionConfirmationEvidence` from an independent observer, and
`AWAITING_SUBMISSION_CONFIRMATION` unreachable by any automatic retry. It left the shape of the
evidence defined and the *observer* undefined: nothing in the system could produce one, so the last
edge of the manual flow was implemented but not reachable in practice.

This ADR is the observer side. It answers three questions that a confirmation feature always gets
wrong if it is written as "search the inbox for 'thank you for applying'":

1. Who observes, and what does the observer depend on?
2. What makes a message a confirmation rather than a coincidence?
3. Where does the observed material live afterwards?

## Decision

### The contract does not know about Gmail

```python
class ConfirmationObserver(Protocol):
    def observe(self, application, *, since) -> list[SubmissionConfirmationEvidence]: ...
```

An e-mail provider is one implementation of a *mailbox port*, not the port itself:

```text
EmailSource.messages_since(since) -> Iterable[EmailRecord]

Gmail ─────────────► GmailEmailSource
fixture/backfill ──► StaticEmailSource        (both satisfy EmailSource)
                             │
                             ▼
                  EmailConfirmationObserver  (satisfies ConfirmationObserver)
                             │
                             ▼
              SubmissionConfirmationEvidence(source=confirmation_email)
                             │
                             ▼
             ApplicationService.confirm_submission()
```

A provider-status observer (JSA-CONF-008) plugs into the same contract and produces the same evidence
type with a different `source`. Nothing in `confirmation.py` names Gmail, and the reconciliation
service accepts any observer.

### Corroboration, not a keyword

The receipt phrase is a **gate**, not a proof: without it there is no candidate at all, and with it
alone the score stays **below** the domain's acceptance floor. Corroborations are independent and add
up:

```text
application_confirmation_phrase   0.35   gate — necessary, insufficient on its own
company_match                     0.20   the company name appears in sender/subject/body
ats_domain_match                  0.20   the sender domain belongs to the ATS (from `providers`)
job_match                         0.20   the job title or the external id appears
time_window_match                 0.05   inside the window — weak on purpose: it only excludes
```

So `phrase` alone scores `0.40` and is recorded but refused; `phrase + one real corroboration` scores
`0.55` and is accepted. The ATS domain set comes from the provider profile
(`submit_origin`) rather than a vendor list duplicated here: a board that changes provider must not
require editing this module.

Rejection language is a **veto**, not a missing signal. "Thank you for your interest… unfortunately"
contains a thank-you and is not a confirmation; a message carrying rejection language produces no
candidate at all.

### The window starts at the report

```text
MANUAL_SUBMISSION_REPORTED  ──►  since = that instant − 15 min tolerance
```

The instant comes from the event journal, not from a parameter: the window must be the one of the
report that exists, not the one the caller remembers to pass. Searching the whole mailbox is
explicitly rejected — an old "we received your application" from the same company would confirm a new
application it has nothing to do with. The tolerance exists for clock skew and provider timestamp
granularity, and it is small on purpose.

### Detected is not accepted

```text
observer finds a candidate
   │
   ▼  persist  ── every candidate, including the ones that will not pass
assert_confirmation_evidence()          (the single domain door, ADR 0005)
   ├── accepted  ──► confirm_submission() ──► SUBMITTED
   └── refused   ──► stays AWAITING_SUBMISSION_CONFIRMATION
```

Everything observed is persisted *before* any decision, in its own append-only table
(`confirmation_evidence`, migration 006). Refusing cannot mean deleting: the row records what was
seen, with which signals and which score, and `accepted` is monotonic because accepting is a
historical fact that a later reconciliation cannot undo. Evidence ids are derived from content, so
reconciling twice does not duplicate anything.

### What is persisted is the evidence, never the message

```json
{
  "source": "confirmation_email",
  "reference": "18f0a1b2c3d4e5f6",
  "provider": "email",
  "observed_at": "2026-09-24T18:12:04+00:00",
  "confidence": 0.60,
  "signals": ["application_confirmation_phrase", "ats_domain_match", "time_window_match"]
}
```

Subject, body, sender and the display name exist in memory during matching and never survive it. The
reference is the provider's message id when that id is already an opaque token (the Gmail API id), and
a digest otherwise — an RFC `Message-ID` can carry the sender's domain, so it is not kept verbatim.
`signals` is what allows the score to be audited instead of believed.

### What is *not* decided here

- No CLI for reconciliation. The operation becomes reachable from outside when a real mailbox source
  exists (JSA-CONF-006); a command that accepted a file or a typed reference would be the free-text
  path ADR 0005 refuses.
- The confidence weights are a **policy**, declared as data in one place. They encode "the phrase is
  necessary but not sufficient" and "corroboration is required"; they are not a statistical model and
  they do not pretend to be one.
- Provider-status observers (JSA-CONF-008) are future work with the same contract and their own
  evidence provenance.

## JSA-CONF-006: Gmail as the first `EmailSource`

### Least privilege is the mechanism, not the intention

```text
requested scope:  https://www.googleapis.com/auth/gmail.readonly
never:            gmail.modify · gmail.send · mail.google.com
```

The agent does not mark as read, move, delete or send anything, and the scope is what makes that
mechanically true rather than a promise in a docstring. `gmail.metadata` alone is **not** enough: the
confirmation requires reading subject and body in memory to find the receipt language and the
corroborations.

The scope is enforced on the way in as well: a token whose recorded scopes contain anything beyond
readonly is refused, and an authorization that grants more than readonly is rejected *before* the
token is written. Credentials that can do more than the task are not the credentials of this system.

### Credential layout, and the gates on reading it

```text
~/.config/jobsearch-agent/gmail/
├── client_secret.json   OAuth client — application configuration
└── token.json           access/refresh token — the user's local secret
```

Directory `0700`, files `0600`, and this is verified **on read**, not only applied on write: a token
that group or other can read is refused, because the right answer to a possibly leaked secret is to
reauthorize, not to keep using it. Both gates run over the JSON document without importing a Google
library, so the privacy policy is testable without credentials and without network.

Neither file ever enters `Application.context`, the event journal, `confirmation_evidence`, artifacts,
logs or stdout. `credentials_summary` exists so the status command can say "this is configured"
without saying what it is configured with.

Access is established by a command of its own, which only establishes access:

```bash
jobsearch-agent integrations gmail authorize     # consent in the normal browser, callback on localhost
jobsearch-agent integrations gmail status        # read-only summary, no secret
```

### Incremental query, and who decides the window

```text
Gmail query `after:<epoch>`  ──►  reduces the universe
message internalDate         ──►  decides: internalDate >= MANUAL_SUBMISSION_REPORTED − tolerance
```

Even when the provider's search syntax already filters by date, the real message timestamp is
validated a second time. The semantics of the window stay decided by this system: if Gmail's query
semantics change, the window does not. A message the query returned but whose timestamp precedes the
window is not evidence — and a message without `internalDate` cannot be placed in time, so it is not
evidence either.

`reference` is the **opaque id returned by the API**, never the `Message-ID` header: the header can
carry the sender's domain, and the evidence must not.

### A failure is not an absence

```text
401 · 5xx · timeout · refused refresh  ──►  ConfirmationSourceUnavailable   (an error)
empty result                           ──►  "no confirmation evidence observed"  (a result)
```

The two are deliberately different types. `ConfirmationSourceUnavailable` propagates through
reconciliation: nothing is persisted, the Application state does not change, and the operator sees a
failure instead of a conclusion. "I could not look" can never be read as "there was nothing there",
and therefore never as "there was no submission".

### Why not IMAP now

IMAP would work, but an app password is a long-lived secret, message ids and search semantics vary
more across providers, and incremental retrieval is less convenient — with worse credential
management for no extra capability here. IMAP remains a future adapter behind the same `EmailSource`
port, useful for Outlook, corporate mail and generic providers.

## Consequences

- The manual flow can finally reach `SUBMITTED` without any user declaration: automation → anti-bot
  rejection → handoff → human finishes → report → independent observer → confirmation.
- A false positive now requires the receipt language **and** an independent corroboration inside a
  window anchored to the report; a false negative leaves the application in
  `AWAITING_SUBMISSION_CONFIRMATION` with the candidate recorded, which is the safe direction.
- Rejections, expired windows and unmatched messages are visible as *absent evidence*, not as a
  failed transition — the state does not change when the observer finds nothing.
- The evaluation of this observer against a real mailbox is JSA-CONF-007 and cannot be done with
  fixtures: it requires a real confirmation e-mail from a real application.
