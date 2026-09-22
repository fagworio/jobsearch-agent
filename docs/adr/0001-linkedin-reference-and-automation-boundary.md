# ADR 0001: LinkedIn reference and automation boundary

- **Status:** Accepted
- **Date:** 2026-09-22

## Context

The requested Easy Apply reference project automates LinkedIn browsing and application
interactions. LinkedIn's published policy prohibits third-party software from automating activity
on its website. The upstream repository also carries a Creative Commons Attribution-NonCommercial-
ShareAlike 4.0 license. Meanwhile, this project's browser dry-run is intentionally network
no-write and its execution plan has no submit action.

## Decision

1. Treat `wodsuz/EasyApplyJobsBot` as a behavioral reference only. Do not add it as a runtime
   dependency or copy its source, selectors, credentials, stealth code, or session implementation.
2. Do not implement a LinkedIn login/session manager, live DOM inspector, form filler, step navigator,
   scraper, or submitter while LinkedIn's published prohibition applies. A state-only
   `LinkedInSession` contract is allowed solely to represent a user-owned browser profile and
   manual handoff; it must not accept credentials, control a browser, or perform network access.
   Reconsider live automation only with written authorization from LinkedIn or an official API
   whose terms permit this use.
3. Keep synthetic fixtures and provider-neutral application state models separate from any live
   LinkedIn access. A manual handoff to the user's browser is permitted as a product direction;
   the agent must not automate LinkedIn interactions behind that handoff.
4. Keep any future supported ATS submission as a subsystem distinct from dry-run. Never turn on
   general POST access or add `submit` to `ExecutionAction`.
5. A future live submission boundary must require an expiring, one-application authorization bound
   to the destination, provider, form fingerprint, resume hash, and answers fingerprint; persist
   an attempt before network I/O; block repeats; classify ambiguous outcomes as `SUBMIT_UNKNOWN`;
   and verify provider confirmation before recording success.

## Consequences

- LinkedIn-specific live automation is deferred, not silently implemented from the reference bot.
- The immediate LinkedIn-compatible milestone is a manual handoff plus local fixtures and factual
  profile/Q&A readiness, without opening or controlling LinkedIn pages. The offline inspector and
  CLI accept only caller-provided HTML snapshots and report `network_access=none`.
- Multi-step navigation, live DOM inspection, upload, and submission remain deferred; a future
  implementation requires written platform authorization or an official API whose terms permit it.
- Greenhouse and other employer-hosted ATS work can proceed independently, but any production
  submission still needs the explicit authorization, scoped network policy, duplicate guard,
  result verification, and local test server described in the submission-boundary roadmap.
- The upstream license is recorded for provenance. No upstream code is copied by this ADR or the
  accompanying audit.

## References

- [EasyApplyJobsBot](https://github.com/wodsuz/EasyApplyJobsBot)
- [EasyApplyJobsBot LICENSE](https://github.com/wodsuz/EasyApplyJobsBot/blob/main/LICENSE)
- [LinkedIn policy on prohibited software and extensions](https://www.linkedin.com/help/linkedin/answer/a1341387)
- [Submission Boundary roadmap](../references/easyapplyjobsbot-audit.md)
