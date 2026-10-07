# Developing while live

Harry, 7 Oct 2026: "ensure that it's easy to continue developing the system whilst live, i.e. that there is a
cohort system in place for contacts that have started not being interrupted by changes. Make this as simple and
robust as possible."

The short version: a contact's emails are fixed when they are enrolled; what every lead in a campaign shares is
held while leads are in flight; safety always applies to everyone; and every contact carries the version of
everything it was enrolled under, so the Monday readout and `us-outbound cohorts` compare like with like.

## What is fixed, what is shared, what always applies

| | What | How it behaves |
| :- | :- | :- |
| Fixed per contact | Every email's subject and body: the Copy row, the signature, the opener, the price line, the links, email 1's subject arm, UTM tags; the sender | Rendered once at enrolment (or when a send-approval card is posted) into the lead's custom variables. Nothing rewrites a lead's variables afterwards (the only lead write is its status). A card waiting in Slack keeps what it shows |
| Shared by every lead in a campaign | The step template (the unsubscribe line and its link), the step delays (`STEP_DAYS`), `text_only` and `first_email_text_only` (from `email_format`) | Drift in these is **held** while the campaign has leads in flight: `campaigns ensure --fix` leaves it, and says so. `--in-flight` applies it to them too |
| Changes for everyone, by design | The daily limit and the sending list (the ramp and the sheet), the send window, the From name, the other campaign settings (always put back to their pinned values), the pause over the blackout dates (the `blackout` job) | Put right as before; every change other than the daily limit and sending list is logged in `config_log` with the leads it reached, the blackout's pause and restart included |
| Always applies to everyone | Unsubscribes, bounces, complaints, kill rules, `stop`, the account-level stop, suppression (a HubSpot opt-out loaded after enrolment, or a company that has become a Spill customer, read nightly, stops the lead in flight), erasure, a booking read back, a step that has lost the unsubscribe link | Never held for a cohort or a version |

"In flight" is a lead in a US Outbound campaign, not stopped, with a step still to send by the send forecast
(`enrol/capacity.in_flight`). `us-outbound cohorts in-flight` lists them per campaign, with the day each
campaign's last one finishes.

## The checklist

1. **Content is per lead and fixed at enrolment.** Copy, the signature, openers, General content keys,
   Industries, Roles, Signals and Overrides reach only contacts enrolled after the next `settings sync` (or
   deploy). Safe at any time. Cards already waiting in Slack keep the version they show. The industry label
   check (`labels.py`; Harry, 7 Oct 2026) is content too: its prompt (`PROMPT_VERSION`), the label list and the
   Industries `definition` and `apollo_keywords` decide which label, and so which copy, a new company earns
   (`labels_hash`, in the config version). An edit to one makes every stored verdict stale: verify_accounts asks
   again about 150 companies a run, so the queue is re-checked over about three weekdays (`us-outbound labels
   audit --live` does it at once). Run `us-outbound labels eval --live` before shipping a change to the prompt,
   the decision rule, a definition or keywords, and put its score in the commit message.
2. **Campaign constants are shared by every lead in flight:** `STEP_DAYS`, the step template
   (`UNSUBSCRIBE_*` in `clients/instantly.py`), `CAMPAIGN_SETTINGS`, `email_format`.
   `tests/test_cohorts.py::test_campaign_constants_are_pinned` fails when you change one. To ship it: bump the
   literal, deploy, then either wait until `us-outbound cohorts in-flight` shows none for the campaign and run
   `us-outbound campaigns ensure --fix --live`, or apply it to everyone in flight with
   `us-outbound campaigns ensure --fix --in-flight --live` (logged, with the lead count; it shows in the
   readout's *Settings changes*). `mailbox_health` and `mailbox check --fix` never apply these. Until then,
   the morning mailbox check says what is held, `golive` warns, and `start` goes ahead (the campaign is
   consistent for its leads in flight). A step that has lost Instantly's unsubscribe link is put back for
   everyone at once: the opt-out is never held.
3. **Safety always applies to everyone:** unsubscribes, bounces, complaints, kill rules, `stop`, the
   account-level stop, suppression, erasure. Never gate these on a cohort or a version.
4. **Measurement changes move every cohort:** a view, the reply classifier or `sync_outcomes` changes the
   numbers for old cohorts too. Say so in the commit; `us-outbound cohorts` prints the code each cohort was
   enrolled under.
5. **DDL is additive** (`CREATE … IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`; `ops/ddl.py` refuses anything
   else) and is applied by `us-outbound db apply --live` on deploy. New columns are nullable; code reads NULL
   as "before this existed" (contacts enrolled before 8 Oct 2026 are "unstamped").
6. **Dry-run first**, then `us-outbound golive`. A deploy while enrol (12:00 UK) or poll_approvals runs is
   fine: jobs are idempotent and the scheduler drains for 30 seconds.
7. **Never rename an owner** on the Mailboxes tab, and never pause a mailbox to "test" something: both move
   leads in flight (the campaign name and `accounts.sender` are the owner's name; Instantly hands a paused
   mailbox's follow-ups to the owner's other mailbox).
8. **Check the config version moved** when you meant it to: `us-outbound cohorts changes` after the sync or
   deploy (enrol records the version when it next runs).

## The config version

Each contact carries `config_version`, `code_sha` and `copy_hash` (`config_version.py`, written by enrol and by a
send approval's ✅). The version is a 12-character hash of: the versions in force of the Signals, Angles,
Industries, Roles and Overrides tabs; each sendable Copy row's content hash; the General keys that shape what is
sent (`config_version.CONTENT_KEYS`: the subject and opener keys, `price_from`, the links, `email_format`,
`send_window`, `utm_links`, the second-contact keys, `control_share`, `weekly_enrol_cap`, `label_check`); the
signature template;
the campaign constants; the label check's `labels_hash` (its prompt version, label list and definitions); and
the code (`RAILWAY_GIT_COMMIT_SHA`, set by Railway on a deploy from GitHub; "dev" elsewhere). An edit to `live_sending`, the Claude cap, a draft Copy row or a mailbox's status starts no new
version: they change nothing a contact is sent. The `config_versions` table keeps each version's snapshot, so
`us-outbound cohorts changes` can say what differs in plain words.

## Cohorts

A cohort is the UK week a company's first contact was enrolled ("2026-W41"), split by config version when a week
has two. A company counts at 7, 14, 21 and 28 days after its email 1 once it has reached that age, so two weeks
are compared over the same days. Rates need 30 companies (a bounce rate 100 sends); under that, the counts only.
A difference between two cohorts names what changed between them: a change worth a pre-registered test on the
Tests tab, not a cause.

Cohorts are read from contacts, and contacts who never replied are deleted 12 months after their last step
(SPEC 6; `ops/retention.py`). So a cohort more than a year old loses the companies left with no contact, and reads as
replying more than it did: compare cohorts within the year. The Monday readout's views count each company from its
events, which stay, so its numbers do not move (only that contact's enrolment snapshot goes, and the signal table
then reads that company's signals as they are now).

## Known effects, not changed here

- A lead's later steps go out from the mailbox that sent step 1 while it stays on the sending list; after a pause,
  Instantly hands them to the owner's other mailbox (PHASE0-CONFIRM, docs/build-plan.md item 7).
- Instantly applies a changed step template or delay at each lead's next step (seen with the footer change of
  6-7 Oct 2026; PHASE0-CONFIRM for delays).
- Instantly's schedule knows weekdays only, so the hourly `blackout` job pauses every campaign over the blackout
  dates and starts the ones it paused again after them (Harry, 7 Oct 2026; docs/daily.md "Blackout dates"): the
  follow-ups that fell due go out on the next send day, as the send forecast assumes. A cohort enrolled just before
  a blackout reaches its 7, 14, 21 and 28 days with fewer of its emails sent; the cohort report lists the pause and
  the restart under *Settings changes*. PHASE0-CONFIRM: that Instantly sends the steps that fell due once a paused
  campaign is activated again, rather than skipping them.
