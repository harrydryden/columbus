# US Outbound: notes for Claude

## Settled decisions (Harry). Do not raise them again.
- **No postal address and no privacy link in the emails** (1 Oct 2026, final). The opt-out is
  Instantly's own unsubscribe link and the List-Unsubscribe header (the link is written as Instantly's
  placeholder `https://UNSUBSCRIBE_INSTANTLY.ai`, which it swaps per lead; `{{unsubscribe}}` went out empty).
- **The signature is outside the copy rules** (1 Oct 2026). Its "Book a call here" link and its
  Trustpilot link are fixed text, allowed in every email, email 1 included
  (`templates/copy/signature.txt`; `copy_rules.email_violations` checks the body only).
- **No data notice in any email** (5 Oct 2026). "Where we got your details" and the legitimate-interests
  text are not shown to prospects; enrol keeps them on each contact (`contacts.data_record`).

## Working rules
- Secrets live only in sealed Railway variables: never in the repo, logs, database or chat.
- Every job is dry-run by default; `live_sending` = yes on the General tab is Harry's sign-off.
- Campaign constants (STEP_DAYS, the step template, email_format) change only through docs/developing-while-live.md.
- Develop on branch `claude/spec-review-build-plan-08kpuz`.
