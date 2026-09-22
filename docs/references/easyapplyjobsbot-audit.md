# EasyApplyJobsBot reference audit

**Reviewed:** 2026-09-22  
**Repository:** [wodsuz/EasyApplyJobsBot](https://github.com/wodsuz/EasyApplyJobsBot)  
**Scope:** README, `linkedin.py`, configuration guidance, and upstream license. This is a
behavioral review, not a code port.

## Observed behavior

The project is a Selenium-based LinkedIn Easy Apply bot. Its published workflow covers job
discovery/filtering, checking whether a job was already applied to, selecting a resume already
stored in LinkedIn, filling phone and saved additional answers, advancing multi-step forms,
reviewing, and clicking the final application button. The README describes automatic login and
automatic application as product behavior.

The implementation documents accessible-label selectors for the Continue, Review, and Submit
steps, and includes heuristics for resume selection and phone inputs. The multi-step path also
uses a completion-percentage heuristic. These observations are useful as a list of UI states to
model in local fixtures; they are not stable selectors or contracts for a supported LinkedIn
integration.

The upstream `dryRun` switch skips the final submit click in the described paths, but the bot
still opens LinkedIn pages and interacts with the application workflow. It is not equivalent to
this project's network-denying `DryRunExecutionPlan` and must not be treated as a security
boundary.

## Reference-only boundary

- No dependency on EasyApplyJobsBot is added to the domain or runtime.
- No upstream source code, selectors, stealth behavior, credentials, or session-handling code is
  copied into this repository.
- Its high-level workflow may inform provider-neutral state modeling and synthetic local fixtures.
- Any later proposal to reuse implementation code requires a separate license review and explicit
  approval; a link to the project is not permission to incorporate its code.

## Important limitations for this project

- LinkedIn's published policy says third-party software must not automate activity on its site.
  Therefore the roadmap items that log in, inspect live Easy Apply DOM, fill fields, advance steps,
  or submit are not implementation targets unless LinkedIn provides written authorization or an
  official supported API for that use.
- Login, MFA, CAPTCHA, and account challenges remain human-intervention states. No bypass or
  stealth mechanism is in scope.
- Resume selection in the reference bot is based on resumes already stored in the LinkedIn
  account; this project instead requires a per-application generated artifact with provenance and
  a verified hash.
- A click or a bot log line is not sufficient proof that an application was accepted. Any future
  supported submission flow needs provider-specific confirmation evidence and duplicate protection.

## Sources reviewed

- [Upstream README](https://github.com/wodsuz/EasyApplyJobsBot/blob/main/README.md)
- [Upstream `linkedin.py`](https://github.com/wodsuz/EasyApplyJobsBot/blob/main/linkedin.py)
- [Upstream LICENSE](https://github.com/wodsuz/EasyApplyJobsBot/blob/main/LICENSE)
- [LinkedIn: Prohibited software and extensions](https://www.linkedin.com/help/linkedin/answer/a1341387)
