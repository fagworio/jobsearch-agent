# ADR 0004: read-only inspection POST boundary

- **Status:** Accepted
- **Date:** 2026-09-24

## Context

Some ATS are SPAs that build the application form from an API response. Ashby fetches the posting and
form schema with `POST /api/non-user-graphql`. There is no HTML form to inspect before that request
happens.

The existing guard blocks every non-read method, so the Ashby board was unreachable by construction:
`apply` refused it with `INSPECTION_REQUIRES_WRITE`. Both obvious ways out are wrong:

- Blocking all writes keeps the boundary intact but leaves the provider permanently unusable.
- Allowing "POST during inspection" trades the boundary away, and would let a GraphQL `mutation`
  through the same endpoint.

The endpoint is the tell: `POST /api/non-user-graphql` carries many operations. URL and method alone
cannot distinguish "fetch the form schema" from "delete a candidate".

## Decision

> **HTTP POST does not imply a semantic write. Authorization depends on the purpose and the content
> of the operation. An inspection POST is permitted only when it is provably read-only and limited to
> the expected provider, endpoint, operation, and resource.**

1. Introduce `InspectionNetworkPolicy` for the `FORM_DISCOVERY` stage only. It authorizes requests
   through `AuthorizedInspectionRequest`, a type **separate** from `AuthorizedWrite`.
2. Do **not** reuse `SubmissionIntent`, `LiveNetworkPolicy`, or the `AuthorizedWrite` budget. A
   write permit means "a mutation is allowed"; an inspection permit means "this specific read is
   allowed despite using POST". Mixing them would let supporting a new provider weaken the boundary
   that already works for Greenhouse and Lever.
3. Give inspection its own budget. Spending or exhausting it must never consume or release upload or
   submission credit — verified by test.
4. Validate content, not just transport:
   - `operationName` must be on the provider's allowlist;
   - the `?op=` query hint, when present, must agree with the body;
   - the document must be provably a query — `mutation` and `subscription` are refused, and an
     unterminated string or block string is refused rather than parsed optimistically;
   - variables must bind exactly to the current job: required bindings must be present and equal, no
     variable outside the declared set is allowed, and the operation cannot address another board or
     another posting.
5. Declare operations in the provider profile (`ProviderProfile.inspection_operations`), including
   which variables are required and which are optional. Bindings are resolved from the current job's
   board and external id, so a permit is valid for exactly one posting.
6. Audit with a closed set of reason tokens (`INSPECTION_REASON_TOKENS`). Only the token, method,
   origin and path hash survive redaction — never the raw query or variables.
7. Keep the native-submit block in place while inspections are armed. The page may fetch its schema;
   it still cannot submit.

## Evidence from the real board

Reconnaissance against `jobs.ashbyhq.com/linear/<posting>` showed the two operations discovery
actually performs:

```text
ApiJobPosting
  variables: organizationHostedJobsPageName=linear, jobPostingId=<posting>

ApiOrganizationFromHostedJobsPageName
  variables: organizationHostedJobsPageName=linear, searchContext=JobPosting
```

With the policy armed, the form loaded with its full field set (`_systemfield_name`,
`_systemfield_email`, `_systemfield_resume`, plus the custom question ids): **3 inspection requests
allowed, 0 writes used, 0 blocked POSTs**.

Two findings shaped the design:

- **Optional variables are real.** The SPA calls `ApiOrganizationFromHostedJobsPageName` both with
  and without `searchContext`. Requiring exact set equality refused a legitimate call of an
  allowlisted operation, so the model separates required from optional bindings: optional variables
  may be absent, but if present they must match, and unknown variables are still refused.
- **An unterminated literal is a bypass.** If the string stripper ran to end-of-document, a
  `mutation` written after an unterminated quote would have been swallowed and the request allowed.
  The check now refuses when a literal is left open.

## Consequences

- The Ashby form becomes inspectable without weakening the submission boundary: a `mutation` on the
  same endpoint is refused even with a permit armed.
- Inspection failures are reported with a specific reason token (`INSPECTION_NOT_A_QUERY`,
  `INSPECTION_RESOURCE_MISMATCH`, `INSPECTION_BUDGET_EXHAUSTED`, ...), so a refusal is diagnosable
  without logging request payloads.
- `form_loaded_by_api_write` no longer implies "unsupported". A provider with declared inspection
  operations is reachable; one without them still gets an explicit `INSPECTION_REQUIRES_WRITE`.
- Opening the form is not submitting it. The `AshbyAdapter`, the read-only host the SPA needs for
  `www.recaptcha.net`, and any upload/submit policy for Ashby remain separate, later work — and
  submission stays bound to `LiveNetworkPolicy` and a `SubmissionIntent`.
