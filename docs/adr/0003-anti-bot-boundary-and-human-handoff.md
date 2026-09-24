# ADR 0003: anti-bot boundary and human handoff

- **Status:** Accepted
- **Date:** 2026-09-23

## Context

The live executor now reaches the real end of a Lever application: it inspects the form, resolves
custom questions, fills and revalidates the DOM, uploads the resume (provider confirmed `200`),
authorizes a single write, and performs the browser-native submit. On the CI&T board the POST did
leave the browser and the provider answered `400` — "There was an error verifying your application."

The cause is the provider's anti-bot check: the submission carried an hCaptcha token the server
refused, and the widget itself reported `navigator.webdriver = true`. The agent therefore hits a
boundary that is not a defect in the executor.

Two behaviours were previously conflated under a single `NEEDS_CAPTCHA` state: a challenge that was
never solved (no write left the browser) and a submission that was sent and rejected by anti-bot
verification. They need different responses and different audit records.

## Decision

1. Keep `NEEDS_CAPTCHA` for a challenge that stopped the run **before** any submission write: the
   form is intact, nothing was sent, and a human can solve the challenge and retry.
2. Introduce `NEEDS_HUMAN_CAPTCHA` for a submission that **was sent** and refused because the
   provider could not verify the browser. It is a terminal handoff, not an automatic retry.
3. Record that outcome as `SubmissionVerification.challenged` with a closed-set reason token
   (`captcha_verification_failed`) plus redacted evidence: `submit_write`, `confirmed_submission`,
   and `status_code`. The domain does not claim a generic `SUBMIT_FAILED`, because the request was
   well-formed and was delivered.
4. Do **not** disguise automation. Hiding `navigator.webdriver`, spoofing plugins, or tampering with
   fingerprints is out of scope: it stops being "driving the form in a normal browser" and becomes
   deliberately concealing automation from the provider. This is the same boundary established for
   LinkedIn in ADR 0001.
5. Allow the challenge widget's own reads and its provider writes, because without them the CAPTCHA
   never loads and "solve it manually" is impossible. These permits are scoped to the anti-bot
   provider's origins and never cover the board's submission endpoint, which remains bound to the
   single authorized `SubmissionIntent`.
6. Keep the default posture autonomous up to the boundary: the agent does not promise that every
   application is submitted autonomously. The contract is "autonomous until a boundary genuinely
   requires a human", never "hide the automation" and never "make the whole system manual".

## Consequences

- A provider anti-bot rejection surfaces as a distinct, operator-visible state with evidence, instead
  of being flattened into a submission failure.
- `NEEDS_HUMAN_CAPTCHA` is excluded from automatic resume; reopening requires the explicit
  `retry-submit` operation, which is recorded. The provider already refused unambiguously, so
  duplicate risk does not apply.
- Capability varies per provider and is expected to: Greenhouse reaches a real `POST` and is stopped
  by reCAPTCHA Enterprise, Lever by hCaptcha/automation verification, Ashby still needs a
  form-discovery write, and LinkedIn Easy Apply remains out of scope. The remaining work is
  provider-specific, not core.
