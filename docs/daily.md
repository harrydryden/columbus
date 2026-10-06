# US Outbound: your day

For Harry, from the pilot that starts on Monday 5 October 2026. Run every command inside the
worker: `railway ssh -- us-outbound <command>`. Nothing changes without `--live`.

## The two switches (General tab)

| Key | no | yes |
| :- | :- | :- |
| `live_sending` | Nothing new reaches Instantly or a prospect. Jobs run dry, and enrol posts a few preview cards to #us-outbound-dev | Emails go out. The sheet refuses yes while `approver_slack_ids` is blank, and no lead is added until `optout_tested` = yes |
| `auto_send` | The pilot: every email waits for an approver's ✅ on its card in #us-outbound | Emails go straight to Instantly once the weekly hand-check is approved |

**Company size (General `min_employees`, `max_employees`; 10 and 249 by default).** To contact companies of 5
to 500, set 5 and 500. From the next sync every search covers the new range (new size bands are searched
from page 1), verify_accounts holds accounts to it, and the hand-check's size edges move with it. The
Roles tab's 10-49 order applies below 10 and its 50-249 order above 249. The first time, run
`us-outbound settings load --tab General --live` to add the two keys to the sheet.

**Sheet edits apply at the next sync:** 02:00 UK every day, and 11:30 UK on weekdays, so a morning
edit is in force for the 12:00 enrol. To apply an edit now, run `us-outbound sync`.
`us-outbound start --live` syncs by itself first. `us-outbound status` says when the settings were last synced.

## The brake

`us-outbound stop --live` pauses every US Outbound campaign in Instantly and stops new enrollment.
**Setting `live_sending` = no does not stop Instantly:** a campaign that is already active keeps
sending its follow-ups. To resume, run `us-outbound start --live`. It syncs the sheet, checks the
campaigns still match the settings, and activates them.

## Your day in #us-outbound (UK times)

| When | What arrives | What to do |
| :- | :- | :- |
| 07:00 | Mailbox health, only when something changed or is wrong: a mailbox promoted to Active, a campaign's daily limit raised with the ramp, a new campaign created or activated, a sender name that is not the owner's full name | Usually nothing. If it asks you to run `us-outbound start --live` or `us-outbound mailbox check --fix --live`, run it |
| Monday 08:00 | The weekly hand-check, only if some accounts have doubtful facts (no HQ state, size or industry, a size near a band edge), site visitors included | `us-outbound handcheck show`, then `us-outbound handcheck approve --live`, adding `--pull DOMAIN` for any that are wrong. A missing fact needs an Overrides row (`hq_state`, `employees` or `industry`); approving alone keeps the account on the check |
| 09:00 | The daily post | Read the **Needs you** line under the headline first |
| 12:00 | Send cards: one per email, with the whole sequence in its thread | ✅ or ❌ each one by the end of the next send day. After that the card lapses and the company goes back to the queue. If a ✅ can't go through yet (sending stopped, a reply waiting too long), the card stays open with a note in its thread |
| Any time | Reply cards, each with a draft | Answer within 2 hours, or the card is re-posted (13:00 to 23:00 UK). Answer within 24 hours: a positive reply waiting longer pauses new sends and is emailed to you |
| Hourly | Kill-rule alerts: a mailbox, an email source or an industry group held back, with the reason | Check it, then `us-outbound killrules show` and `us-outbound killrules clear ID --live` |

## The words and emojis

Reply in the card's thread, or react to the card. Only an approver counts: `approver_slack_ids`,
and a mailbox's owner for replies to their own mailbox. The bot puts ✅ and ❌ on each card, so
one click decides.

| On a send card (12:00) | On a reply card | What it does |
| :- | :- | :- |
| ✅ or `send` | ✅ or `send` | Sends it. A send card's lead goes to Instantly within 5 minutes |
| ❌ or `skip` | ❌ or `skip` | A send card offers the three choices below. A reply card is closed, and nothing is sent |
| ✏️ (or `edit`), then a thread reply with the new email 1. Put "Subject: …" first, or "Email 2:" to change a follow-up | `edit: …` | The edit is checked against the copy rules and posted again. ✅ on it sends it |
| — | `send: …` | Sends that text instead of the draft |
| 👤 | — | Not this person: the next-ranked contact at the company is proposed later |
| 🚫 | — | Drop the company for good |

## Expected volume in the pilot

Each sender takes new contacts at a quarter of its daily cap, so the follow-ups on days 7, 14 and
21 always fit. In week 1 the ramp holds each mailbox to 10 sends a day, which is about 3 new
contacts a day per mailbox. Expect about **6 cards a day** with Hannah's and Sam's mailboxes, and
about **11** once Harry's two are warm. Volume grows as the ramp rises to 20 sends a day (about 5
new contacts a mailbox) and then 30 (about 8). In the pilot, `weekly_enrol_cap` (150) is well
above this.

## When something is wrong

- **Stop everything:** `us-outbound stop --live`. Resume with `us-outbound start --live`.
- **A kill rule fired:** `us-outbound killrules show`. Once you have checked it,
  `us-outbound killrules clear ID --live` lifts the hold.
- **A mailbox misbehaves:** `us-outbound mailbox pause ADDRESS --live` takes it off its campaign's
  sending list.
- **The mailbox check says a sender name is wrong:** prospects see each mailbox's From name, and it
  should be the owner's full name from the Mailboxes tab ("Hannah Spalding"), not "Hannah at Spill".
  `us-outbound mailbox check` lists what it would change; `us-outbound mailbox check --fix --live`
  sets it in Instantly (the account's first and last name only).
- **A card says "Not sent":** the re-check at your ✅ found the person or company can no longer be
  emailed (an unsubscribe, a customer or open deal in HubSpot, a suppressed domain). Nothing was
  sent and the card is closed; there is nothing to do. A card that is only *held* (sending stopped,
  a reply waiting too long) stays open and your ✅ stands: the lead is added within 5 minutes of the
  hold clearing, unless the card expires first.
- **The daily post says a campaign is not active:** that sender gets no new cards until
  `us-outbound start --live` activates it. The 07:00 mailbox check activates a newly created
  campaign by itself once sending has gone live.
- **A missed-heartbeat alert:** a job has not run when it should have. `us-outbound status` lists
  the jobs that failed or missed, with the error. The worker's logs are in Railway.
- **Someone asks to be forgotten:** `us-outbound erase --email ADDRESS --live`, then do the manual
  steps it prints.

## Website visits

`site_visits` runs at 06:00 UK every day. It asks Apollo which companies visited spill.chat's US
pages (General `site_visit_us_paths`, `/us`) and its pricing and demo pages (`site_visit_intent_paths`)
in the last 30 days. A visit lifts that company's score before the 12:00 enrol ("Visited the US site"
+35, "Viewed US pricing or demo page" +25, so both make Priority). A new visitor is looked up in
Apollo: one in the US (in any state: a visitor is never excluded on its state) within the General size
range joins the queue, and one Apollo can't place (no HQ state or country,
no size, no industry, a size near an edge) comes in held for Monday's hand-check rather than being
left out. It costs about 3 Apollo credits a day, plus 1 for each new visitor looked up. The daily
post's Sources section has one line on it. The emails never mention a visit.

**If the daily post says "No website-visitor data from Apollo for spill.chat",** check the tracker.
The jobs only read Apollo's visitor list and never touch the tracker, so this is done by hand:
- In Apollo, Settings → Website Visitors: spill.chat is listed and shows data received, and the plan
  includes website visitors.
- Open spill.chat/us in a browser with the developer tools' Network tab open: a request goes to
  Apollo when the page loads. If none does, the tracking script is missing from the US pages.
- While you are there: the intent path should read `/us/pricing` (it was `us/pricing`, without the
  slash), `/us/book-demo` should be added as high intent, and the script should not be on
  employee-facing pages.

## Weekly

- **Focus tab:** the industry mix (Tech 50%, Agencies 30%, Legal 20% to start). Change the shares there.
- **Copy:** approve rows on the Copy tab (`status` approved, `approved_by`). Editing a row clears its
  QA pass, so run `us-outbound copy qa --live` after an edit, then `us-outbound sync`.
- **Volume:** raise `weekly_enrol_cap` on the General tab as the ramp rises and the exit criteria
  hold (docs/roadmap.md §3).
- **Email 1's subject split** (5 Oct 2026): half the companies (General `email1_subject_share`, 0.5)
  get the personal subject `email1_subject` ("support for the {{company}} team"; `{{company}}` and
  `{{first_name}}` only) in email 1 instead of the Copy row's. Emails 2 to 4 are unchanged. Each
  company keeps its arm, and `us-outbound signals review` says which subject gets more replies once
  each arm has 30 companies emailed. To change the subject or the share, edit them on the General tab,
  then `us-outbound sync`; 0 sends every email 1 with the Copy row's subject. The first time, run
  `us-outbound settings load --tab General --live` to add the two keys to the sheet. To see the
  personal subject: `copy preview --subject personal`, or `seed send ADDRESS --owner NAME --subject
  personal --live` for a seed inbox.

## Monthly

- **Lookalikes (the 1st, 02:30 UK):** the `lookalikes` job reads Spill's customers from HubSpot (read
  only), their 12-month headcount growth from Apollo (counts only, at most 60 credits), and works out
  each account's lookalike fit (industry, size and growth). Nothing to do. `us-outbound lookalikes show`
  lists the customers by industry and size; `us-outbound lookalikes fit` shows the fits and the tier
  mix the lookalike rows give, without changing anything. To put the graded rows on the sheet, run
  `us-outbound settings load --tab Signals --live`: it adds "Close match" and "Some match to Spill's
  customers" and switches "Looks like Spill's customers" off.
- **The 1st, 02:50 UK: lookalike leads** (`lookalike_leads`). Apollo looks for US companies like
  Spill's active customers, UK customers included, in the Focus tab's industries and the same size
  band, and the new ones join the queue. They still go through the usual checks (HQ state, size,
  HubSpot, the hand-check if a fact is doubtful), and they score the "Found as a lookalike of a
  customer" signal (+10). Nothing arrives in Slack on its own: that morning's daily post says what
  it found under **Sources**, with how the UK customers' searches did beside the US customers'.
  It spends at most 60 Apollo credits. `us-outbound run lookalike_leads` runs it by hand; a second
  run in the same month does nothing, since the customers are much the same.

## Where the data is

The companies and contacts live in the Postgres database on Railway, schema `us_outbound`: the
`accounts` table (one row per company, by its domain) and `contacts` (one row per person). Beside
them are `signal_events` (what the sources found), `events` (sends, replies, bounces, opt-outs),
`hitl_items` (the Slack cards) and `suppression`. The database has no public access, so look at it
with `us-outbound accounts`: a summary, then the companies in queue order (`--status`, `--tier` and
`--industry` narrow the list). `us-outbound accounts acme.com` shows one company in full, with its
contacts' emails. For a spreadsheet, run `railway ssh -- us-outbound accounts --csv > companies.csv`
on your own computer; it holds names and emails, so keep it private and delete it when you are done.

**People leaders** (5 Oct 2026): each weekday at 04:20, before the queue is sorted, `apollo_people`
looks up who leads People at every company in the queue in Apollo's free people search, so "New
People leader" and "People leader in place" count for companies nobody has contacted yet. "First
People hire (likely)" counts only where Apollo knows at least half the company's staff, so that
finding no People leader there means something. Once, after this reaches the worker:
`us-outbound settings load --tab Signals --live`, then `us-outbound sync`.

**Don't edit rows by hand in Railway's Data tab.** An edit there bypasses the system's checks
(suppression, one company per domain, a sender kept for life). Make changes with the sheet and the
commands instead.

## The commands

| Command | What it does |
| :- | :- |
| `status` | The switches, when the settings were synced, what waits for you, the jobs that need a look, mailboxes, today's number |
| `golive` | The read-only go/no-go check |
| `accounts`, `accounts DOMAIN`, `accounts --csv` | The companies and contacts we hold: a summary and the list, one company in full, or a spreadsheet. Read-only |
| `sync` | Brings sheet edits into force now |
| `start --live` | Syncs, then resumes the campaigns and enrollment |
| `stop --live` | The brake |
| `seed send ADDRESS --owner NAME --live`, `seed check` | The seed-inbox test of the unsubscribe link (`--subject personal`: email 1 with the personal subject) |
| `approvals list`, `approvals send ID --live` (or `contact ID`, `company ID`) | Send cards without Slack |
| `replies list`, `replies send ID --live` (`--text "…"` sends your text), `replies skip ID --live` | Reply cards without Slack |
| `killrules show`, `killrules clear ID --live` | Kill-rule holds |
| `mailbox check --live --fix` | Mailbox health now, each sender name set to its owner's full name, and the campaigns put right |
| `copy preview --industry "Fintech" --html fintech.html`, `copy qa --live` | An email as a prospect will see it; QA for edited rows |

Also `handcheck show|approve`, `erase --email` and `schedule`. `us-outbound --help` lists every command.
