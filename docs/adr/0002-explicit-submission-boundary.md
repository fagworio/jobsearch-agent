# ADR 0002: explicit submission boundary

- **Status:** Accepted
- **Date:** 2026-09-22

## Context

The browser dry-run can inspect, resolve, fill, and upload, but it must never submit. A
`READY_TO_APPLY` decision means that technical and factual gates passed; it is not user permission
to transmit an application.

## Decision

1. Keep `ExecutionPlan`/`DryRunExecutionPlan` limited to `fill` and `upload`, with
   `STOP_BEFORE_SUBMIT` as the terminal action.
2. Represent provider navigation separately as `LiveApplicationPlan`; `advance` is allowed, but
   submission is not an execution action.
3. Bind live submission to a persisted `SubmissionIntent` containing the application, job, provider,
   exact destination, form fingerprint, resume SHA-256, answers fingerprint, creation time, and
   expiration time.
4. Require an explicit authorization transition before a request can start. The attempt is written
   transactionally before network I/O and moves the application to `SUBMITTING`.
5. Allow network writes only through a provider-specific `LiveNetworkPolicy`. Unexpected origin,
   path, method, or stage is blocked.
6. Require provider confirmation evidence for `SUBMITTED`. A timeout after the request becomes
   `SUBMIT_UNKNOWN`; `SUBMIT_UNKNOWN`, `SUBMITTING`, and `SUBMITTED` block retries.
7. Store only redacted evidence metadata. Response bodies and arbitrary provider payloads are not
   persisted.

## Consequences

- `authorize-submit` does not perform a network request; it only changes the persisted state to
  `SUBMIT_AUTHORIZED`.
- A Greenhouse test-server executor is now available for controlled provider writes; production use
  still requires an authorized intent and the provider-specific policy.
- The CLI `application submit` requires the intent and all current fingerprints explicitly. It does
  not accept a generic destination or a generic provider.
- LinkedIn remains outside active automation. Its supported v1 handoff ends at manual review and
  manual submit, per ADR 0001.
