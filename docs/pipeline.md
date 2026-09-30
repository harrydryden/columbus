# How an account is found, qualified, cleaned and enriched

Apollo, Clay and Instantly can each find companies, find emails and enrich records, and Apollo, Clay, Instantly and HubSpot each have their own idea of an ICP. This page covers:
- which tool does which job, and where the ICP lives;
- the order in which an account is found, checked and paid for;
- the credit budgets and the weekly target, and how the system shows which one is holding it back;
- how many emails Instantly can send, and what it reports back;
- how Harry can steer the system from the sheet.

Harry's decisions on 30 Sep 2026:
- He approved changes 1–8.
- Credit budgets are monthly, like Apollo's and Clay's own: 2,000 each, about 500 a week.
- The enrolment target is weekly: 150, Monday to Sunday, UK time.
- Steps go a week apart.
- The sheet gets an industry focus and named companies.
- Lookalikes wait.

The list of changes is at the end.

## The short answer

- **The sheet is the only ICP.** No persona, saved search, list, score or ICP profile is set up inside any vendor. Every call builds its filters from the sheet when it runs.
- **Each job has one owner.** A second vendor is only ever a fallback, called when the first one misses.
- **Fit finds accounts, and signals rank them.** The main driver is an Apollo organisation search built from the sheet's Industries, States and size bands. Signals never add a company to the universe, with one exception: a company that visits the US site.
- **Free checks come before paid ones.** Clay, the scarcest budget, is spent only on accounts that have passed every free check. An Apollo email reveal is spent only on the contact about to be emailed.
- **Budgets are monthly and paced by the day.** Each job that spends credits checks the month's balance and today's share of it before every batch. Enrolment spends none.
- **Postgres is the only record.** Vendors return observations. Python stores each one as a fact with its source and date, and makes every decision.

## One owner per job

| Job | Owner | Fallback | Not used |
| :- | :- | :- | :- |
| Find companies (the universe) | Apollo organisation search | IRS BMF for nonprofits (January). New site visitors. The Named accounts tab. Great Place To Work and B Corp lists (phase 3) | Clay Find Companies, Instantly SuperSearch, Apollo lists and saved searches |
| Company facts (size, HQ, NAICS, founded) | Apollo | Clay confirms HQ state and size. If they disagree, the account goes to the hand-check | Clay's other data providers; HubSpot auto-enrichment |
| Clean company name | Clay AI, checked against the `clean/names.py` rules | The `clean/names.py` rules alone | |
| Read careers, benefits and values pages | Clay "US Outbound – Accounts" | None: a failed read is scored as "not read" | |
| Funding | Apollo | Clay's Company Latest Funding, run only when Apollo has nothing | |
| Hiring counts | Apollo organisation search with job filters (`apollo_jobs`) | | |
| Job-posting text | The public Greenhouse, Lever, Ashby and Workable feeds (free) | | |
| People at the company (titles, locations) | Apollo people search (free) | | Clay Find People, Instantly SuperSearch |
| Work email | Apollo bulk match, verified emails only | Clay "US Outbound – Contacts" (the Work Email waterfall), for misses and catch-alls only | Apollo's own waterfall (its results arrive only by webhook, and there is no public endpoint); Instantly's lead finder |
| Email verification | Whoever found the email: Apollo's status, or Clay's check | | Instantly verification, which would spend Instantly credits |
| Site visits | Apollo website visitors | Harry's weekly CSV export | Instantly's website-visitor feature |
| Score, tier and angle | Python and the Signals tab | | Apollo scoring, Clay scoring, HubSpot lead scoring and ICP tiers |
| Sending and warmup | Instantly | | Instantly Unibox, AI reply agent, CRM, lead finder and verification |
| Warm leads | HubSpot (the jobs write, only after a positive reply) | | The Apollo–HubSpot sync; Clay writing to HubSpot |

Instantly finds, enriches and verifies as well (SuperSearch, a seven-provider waterfall, and verification at 0.25 credits a lead). This system uses none of that. It would mean paying for a third email source (SPEC 1.1), and a third place where the facts about an account could live.

## Where the ICP lives

| Question | Sheet tab | How the vendors see it |
| :- | :- | :- |
| Which industries? | Industries: labels, NAICS prefixes, excluded NAICS, Apollo keywords | Built into the filters of each Apollo search. Python then re-checks every result against the tab, so a loose Apollo filter can't let a wrong company in |
| Where? | States (HQ state), and CA and WA never | Apollo `organization_locations`. People are checked against the same list |
| How big? | 10–249 employees in four bands (General, code) | Apollo `organization_num_employees_ranges` |
| Who do we email? | Roles: titles, first choice by size, fallback order | Apollo people-search title filters |
| What makes an account better or worse? | Signals, Angles | Never sent to a vendor. Clay reports what the pages say and never judges them (SPEC 8) |
| Who is never contacted? | Hard exclusions (code), Overrides, suppression | Checked in Python before any credit is spent |

The vendors' own ICP features all stay off:
- Apollo: personas, the Context Center profile, scoring and lists. Apollo is read only in any case.
- Clay: Audiences and Signals. Clay Signals is a phase 4 source.
- Instantly: SuperSearch filters.
- HubSpot: the "Ideal customer profile tier" property and lead scoring. The jobs write only `us_outbound_tier`.

## The funnel

Each stage lists what it spends and the status it leaves the account in. A stage starts only on accounts the previous stage passed.

**1. Find: `source_universe` (weekly, Proposed 9).**
- Apollo searches the slices of active industry × active state × size band. Cost: 1 Apollo credit per 100 companies.
- **Proposed 9:** each week covers a quarter of the slices, so every slice is refreshed every four weeks and the month's Apollo budget is spent evenly instead of in one large bill on the 1st.
- The same front door (`accounts.admit`) serves every other way in: new site visitors, IRS BMF matches, the Named accounts tab, and later the GPTW and B Corp lists. Each is matched to an Apollo record by domain.
- At the door, each company gets its root domain, its name is cleaned by rule, and its industry label and group are set from the Industries tab.
- It is deduplicated on root domain against accounts and aliases, against suppression, and against the whole HubSpot portal.
- It is rejected at once for:
  - no domain
  - HQ in CA, WA or an inactive state
  - partner NAICS or keywords
  - HubSpot conflicts
- **Status: `new`.**

**2. Free signals: daily and weekly.**
- **apollo_people** (free): people leaders, US headcount by state, the CA/WA share, and the candidate contacts for the Roles tab.
- **apollo_jobs**: open roles and open People roles.
- **Site visits.**
- **Layoffs.**
- **IRS BMF.**
- **Calendar.**

These give a provisional score. Free data can decide every hard exclusion:
- fewer than 5 US people
- more than 20% of US staff in CA or WA
- founded less than 2 years ago
- HubSpot activity

An account also needs at least one candidate contact: a person matching the Roles tab for its size, located in the US outside CA and WA. **Status: `queued`** once every free check passes, or `disqualified` with the reason.

**3. Verify in Clay: weekdays, `verify_in_clay`, within today's share of the month's Clay budget.**
- It takes `queued` accounts in order of provisional score, enough to keep the verified queue 10 working days deep. With an industry focus set, it takes each group's share. It stops when today's share of the month's Clay credits is spent.
- It runs Apollo organisation enrich (1 credit) first, only if the search record is missing a field Clay needs.
- It then runs the Clay Accounts function in batches of up to 100. The function returns the clean name, confirmed HQ and size, an industry label, the pages it read, benefits, mental-health provision, culture statements, and funding (only when Apollo had none).
- If Clay and Apollo disagree on HQ state or size band, the account goes to the hand-check.
- Afterwards the job-post feeds read the careers URL that Clay found (free).
- It rescores, which sets the final tier and angle. **Status: `verified`.**

**4. Contact: weekdays, `pick_contacts`, just in time, within today's share of the month's Apollo budget.**
- It takes only the accounts the next two enrolment days will use, not the whole verified queue.
- It refreshes the candidates with a free people search, picks one by the role rule, and runs Apollo bulk match (verified emails only).
- On a miss or a catch-all, it runs the Clay Contacts function.
- The email must not be a personal domain, suppressed, or a customer domain. If it fails, it tries the next candidate in fallback order.

**5. Enrol: weekdays at 12:00, `enrol`.**
- It works out today's number (below), re-checks HubSpot, assigns the test version, renders the copy, and adds the lead to its sender's campaign. **Status: `enrolled`.**

### Why fit, not signals, finds accounts

A search built from signals ("funded in the last 18 months", "hiring a People role") would give a smaller, hotter list. But it would leave out the Control tier: the 15% of sends that go to accounts with no signal. Without the Control tier, the Monday readout can't show what any signal is worth. So the universe is every company that fits, and signals only order it.

## Budgets and targets

Credit budgets are monthly, a calendar month in UK time, because that's how Apollo and Clay count credits. The enrolment target is weekly, Monday to Sunday, UK time. The General tab holds:

| Key | Default | Spent or used by | Checked |
| :- | :- | :- | :- |
| `apollo_monthly_credits` | 2,000 (about 500 a week) | `source_universe` (search pages), `verify_in_clay` (enrich), `pick_contacts` (email reveals) | Before every batch: the month's balance, and today's share of it |
| `clay_monthly_credits` | 2,000 (about 500 a week; 0 means no Clay calls) | `verify_in_clay`, `pick_contacts` (Clay Contacts) | The same |
| `apollo_floor` | 5,000 | All Apollo spend | Stops Apollo spend if the account balance falls below it |
| `claude_monthly_cap_usd` | $10 | Reply classification and drafts | A UTC month, as the Anthropic Console counts it |
| `weekly_enrol_cap` | 150 | `enrol` | Each send day takes what's left of it ÷ the send days left in the week. A short Monday is made up by Friday |

**Today's share.** Each weekday may spend what was left of the month that morning ÷ the weekdays left, today included. The budget then lasts the whole month instead of going in the first week. A quiet day leaves more for the rest of the month. Unspent credits don't carry over.

**How the month is going** shows the same figures for each budget:
- used and left;
- the month's pace to date (the budget spread evenly over its weekdays, through today);
- whether spend is ahead of, on or behind that pace (more than 10% either side counts);
- where the month is heading at the pace so far;
- how much today may still spend.

For example:

```
Apollo: 1,007 of 2,000 credits used this month (50%); pace to date 1,818, so behind pace; heading for 1,108 by 31 Oct; up to 326 more today (3 weekdays left).
```

The budgets are not part of today's enrolment number. The enrol job spends no credits, so a budget that has run out never holds back accounts that are already verified and have an email. It limits the stages before enrolment, and when that leaves too few accounts ready, the limiter says so (below).

**Is 2,000 Apollo credits a month sustainable?**
- **Balance:** 30,128 credits were left on 29 Sep, and they reset on 21 Aug 2027.
- **Use until the reset:** about 10.7 months × 2,000 = about 21,400.
- **What that leaves:** about 8,700, which is about 3,700 above the floor.

The monthly 2,000 has to cover:
- the sweep;
- organisation enrichment for each account sent to Clay;
- about 650 email reveals.

Phase 1 measures the sweep.

**Clay:** the month's verified accounts are about 2,000 ÷ credits per account (measured on the first 100 accounts). To keep 150 accounts a week ready, the budget needs roughly 650 × credits per account ÷ the share that passes Clay's checks each month.

**In the sheet:** keep `apollo_monthly_credits` and `clay_monthly_credits` and set both to 2000. Rename `daily_enrol_cap` to `weekly_enrol_cap` with 150. A row still named `daily_enrol_cap`, `apollo_weekly_credits` or `clay_weekly_credits` is rejected, with the new name.

## Where the limit shows

Today's number is the smallest of three terms:

| Term | What it is | What raises it |
| :- | :- | :- |
| **Weekly target** | What's left of `weekly_enrol_cap` ÷ the send days left this week | A higher `weekly_enrol_cap` |
| **Sending capacity** | Each sender's new leads today, after the follow-ups already due and Instantly's backlog (next section) | Another mailbox, or a higher daily cap once warm |
| **Ready accounts** | Verified accounts with a sendable email | Whatever is short behind it (below) |

When ready accounts is the limit, the explanation walks back through the stages and names the first one that is short:
1. verified accounts still waiting for an email, and whether Apollo's budget is used (for today, or until the 1st);
2. Clay has no monthly budget;
3. Clay's budget is used (for today, or until the 1st) while accounts wait;
4. accounts are waiting for Clay;
5. nothing is waiting for Clay, so the universe or the free checks are the limit.

When sending capacity is the limit, it names each sender that is full and why:
- Instantly says the campaign or its inboxes hit their daily limit;
- its inboxes sent 95% of their cap on the last send day;
- follow-ups already fill a day.

It then says to add a mailbox, and how many ready accounts are waiting for inbox space. For example:

```
Add a mailbox for Hannah Spalding: Instantly says the campaign reached its daily limit. `us-outbound mailbox add <address> --owner "Hannah Spalding" --live`, then it warms for 21 days.
92 ready accounts are waiting for inbox space.
```

The same picture appears in several places:
- **`us-outbound status`** has two blocks: "Credit budgets this month" (one pace line per budget) and "Enrolment this week" (today's number, what limits it, each sender, the focus, and what stands behind it).
- **The enrol job's summary** (`limited_by`, `limits`, `focus`), kept in its heartbeat row and in the Railway log.
- **The mailbox check's summary** each morning: limits it set, and campaigns Instantly says are held back.
- **`v_budgets`** in Postgres: each budget this month, with `expected_by_now`, `projected` and `pace`.
- **The daily post** (phase 3, Slack): the `limited_by` line, every morning. Until Slack is set up, it goes to the log.
- **The Monday readout** (phase 3): how many days each term was the limit, and each budget's pace (**Proposed 11**, with alerts when a budget runs ahead of pace, or when the same limit binds three send days running).

## How many emails Instantly can send, and what it reports back

Instantly decides the moment each email goes. The jobs decide how many new leads go in, set the limits Instantly works within, and read back what it did:

| Limit or report | How the jobs use it |
| :- | :- |
| Each mailbox's daily cap | Set on the Mailboxes tab (`daily_cap`, 30). The morning check reads Instantly's own limit on each inbox and sets it back to the sheet's cap when they differ (live; reported in dry-run). Caps are changed in the sheet, never in Instantly |
| The campaign's daily limit | Set to the sum of the owner's Active caps, and checked for drift every morning (`campaigns ensure --fix`) |
| Sends per inbox per day | `GET /accounts/analytics/daily`, filtered to the registry inboxes, read each morning for the last 7 days. If yesterday's sends fall short of what the forecast had due, Instantly is behind, and the shortfall comes off today's room |
| Why a campaign isn't sending | `GET /campaigns/{id}/sending-status`, read each morning. "Daily limit reached" (the campaign's, or every inbox's) marks the sender as full |
| The send window | Mon–Fri 09:00–16:00 ET. No sends at weekends or on blackout dates |
| Step timing | Days 0, 7, 14 and 21. Instantly counts delays in calendar days and moves a step due at the weekend to Monday. A week apart, every step falls on the same weekday as the first, so none does |
| Warmup | Instantly. Warmup emails are separate from the campaign limit, and warmup stays on (SPEC 13) |

**The forecast** (`enrol/capacity.py`) works per sender, because each account keeps its sender for life:
- Every enrolled lead holds a slot on the days its later steps go out. Leads that replied, bounced or unsubscribed hold nothing.
- A new lead sends four emails, so a sender takes at most **capacity ÷ 4** new leads a day. That pace keeps a full day steady.
- It takes fewer when, on any of the four days a lead enrolled today would send, the follow-ups already due (plus any backlog) leave less room.
- So no email, old or new, ever has to wait for a full inbox.
- New accounts go to the sender with the largest share of today's pace left, so Harry's two mailboxes take twice Hannah's or Sam's share.

**Why the steps are a week apart.** With SPEC 10's days 0, 3, 8 and 15, most later steps of leads enrolled Wednesday to Friday fell at a weekend and piled onto Mondays. Simulating 12 weeks with the four inboxes (120 sends a day):

| Cadence | New accounts a week | Sequence length |
| :- | :- | :- |
| Days 0, 3, 8, 15 (SPEC 10) | about 61 | 15 days |
| **Days 0, 7, 14, 21 (now)** | **150**: every inbox full every day | 21 days |

The copy doesn't change, only the gaps. The readout counts a reply for a week after the last step: 28 days from step 1, where SPEC 12 had 21 days for its 15-day sequence. The cadence is set in one place (`clients/instantly.py`, `STEP_DAYS`), and the campaigns, the forecast and the reply window all read it.

**Not yet counted:** the plan's own caps (emails a month and uploaded contacts, shared with the EU campaigns in the same workspace). Phase 0 checks whether Instantly's API reports the workspace's plan usage. If it does, it becomes a fourth term; if not, a General key `instantly_monthly_emails` holds this system's share (**Proposed 14**).

## Where the copy lives

- **The copy is in the sheet's Copy tab:** one row per version and step (`copy_version`, `angle`, `step`, `subject`, `body`, `status`, `approved_by`). The defaults load as `draft`, and only rows with `status = approved` are ever sent (SPEC 5, 10).
- **The enrol job writes each lead's four emails.** It takes the approved version for the account's angle (or the running test's split), fills in the account's opener, proof point, price line, demo line and the sender's signature, and checks every copy rule. Any breach skips the account.
- **It uploads them to Instantly as that lead's own custom variables:** `{{s1_subject}}`, `{{s1_body}}` … `{{s4_body}}`.
- **The three campaigns are created by the jobs** (`us-outbound campaigns ensure --live`), paused, one per sender. Each has four steps whose subject and body are just those placeholders, with the settings from SPEC 9 and the step days above.
- **So nobody writes sequences in Instantly by hand.** A sequence edited there would be reported as drift the next morning. To change what prospects read, edit the Copy tab and approve it.
- **Phase 0 tests Instantly's custom-variable length limit.** If a rendered body is too long, the fallback is smaller variables (`{{opener}}`, `{{proof}}`, `{{ask}}`, `{{price_line}}`, `{{signature}}`) with the fixed text in the campaign step (SPEC 9).

## How Harry can steer the system

**Levers in the sheet:**

| Lever | Tab | Effect |
| :- | :- | :- |
| Focus the week on some industry groups | **Focus** (new): `industry_group`, `share` (60% or 0.6), `note` | Each listed group gets its share of the week's enrolment, in queue order within the group. The groups not listed share what's left of 100%. If a group has too few ready accounts, the rest of the day goes to the next accounts in queue order, so inboxes are never idle. The shares may not add up to more than 100%, and each group needs an active industry on the Industries tab |
| Approach specific companies | **Named accounts** (new): `domain` (a root domain like acme.com), `name`, `note` | Each domain comes in through the front door (same dedupe, suppression and partner checks) and gets the "Named by Harry" signal (+30, editable on the Signals tab). It still has to pass every other check, including HubSpot and Clay. Taking a row off stops the signal |
| Turn an industry on or off | Industries: `active` | The universe search and the queue include it or leave it out. For only some industries, switch the others off |
| Put an industry first | Industries: `priority` | Its accounts go ahead of others at the same score and size |
| Widen or narrow an industry | Industries: `naics_prefixes`, `exclude_naics`, `apollo_keywords` | Changes the Apollo filters and the Python re-check |
| Where | States | HQ states in or out (CA and WA never) |
| Who | Roles | Titles and who comes first by company size |
| What counts | Signals | Weights, new keyword signals on an existing source, Hold and Exclude rules |
| A single company | Overrides | Any field for one domain, winning over every source |
| Mix | General: `priority_threshold`, `standard_threshold`, `control_share` | How tiers are cut, and the share kept for the control group |

**Adding the two new tabs to your sheet.** Add a tab named `Focus` with the header row `industry_group | share | note`, and a tab named `Named accounts` with `domain | name | note`. Until they exist, the sync reads them as empty and carries on.

**Lookalikes** (a Seeds tab of companies to find more like) wait, as Harry decided.

## Changes for Harry

| # | Change | Why | SPEC | Status |
| :- | :- | :- | :- | :- |
| 1 | A free "someone to email" check before Clay | Clay credits are never spent on an account that can't be emailed | 9 | Approved 30 Sep; built in phase 1 |
| 2 | The Clay Accounts function takes `apollo_funding_date` and skips Company Latest Funding when it is set | Saves Clay credits | 8 | Approved; for the function build |
| 3 | One resolver with the precedence table (below) | Stops the last job to run from deciding an account's HQ, size or name | 13 | Approved; phase 1 |
| 4 | One owning source per fact (`open_roles` from `apollo_jobs`) | Removes three-way disagreement | 5, 7 | Approved; phase 1 |
| 5 | `pick_contacts` reveals emails only for the next two days | No credits spent on accounts later re-ranked or dropped | 9 | Approved; phase 2 |
| 6 | Apollo organisation enrich only when needed | Up to one Apollo credit saved per verified account | 7 | Approved; phase 1 |
| 7 | Vendor features stay off; check HubSpot auto-enrichment without changing it | Keeps one record and one ICP | 1.2, 3 | Approved |
| 8 | Decide whether `catch_all_valid` is sendable; test Instantly's "risky contacts off" | Otherwise a catch-all could be enrolled and never sent | 8, 9 | Approved; the test is in phase 0 |
| — | Monthly credit budgets (2,000 each) paced by the day, with the month's pace shown; a weekly enrolment target (150) | Harry | 5, 9 | Done |
| — | The send forecast replaces "caps ÷ 4", with Instantly's reports (limits, sends, sending status) and an "add a mailbox" flag | Harry | 9 | Done |
| — | Steps on days 0, 7, 14, 21; reply window 28 days | 150 a week instead of about 61 | 10, 12 | Done |
| — | Focus and Named accounts tabs | Harry | 5 | Done |
| 9 | A weekly universe sweep over a quarter of the slices | Even Apollo spend through the month | 9 | Proposed (phase 1) |
| 11 | Limit and budget lines in the Monday readout, with the two alerts | Shows the bottleneck without asking | 12 | Proposed (phase 3) |
| 13 | A Seeds tab for lookalikes | More companies like the best ones | 5, 7 | Deferred |
| 14 | The Instantly plan's caps as a fourth term, from its API or `instantly_monthly_emails` | The workspace is shared with the EU campaigns | 1.2, 9 | Proposed; the phase 0 check first |

## Which value wins

Sources write facts only. One resolver sets the account's columns from the facts, before every scoring run, by this precedence. Without it, the last job to run would win, so the Apollo sweep would overwrite what Clay had just confirmed.

| Field | Order |
| :- | :- |
| clean_name | Override → Clay's name, if it passes our rules → Apollo's name after our rules |
| legal_name | Override → Clay |
| domain | It is the key. If Clay's `domain_confirmed` is a different root domain that is not a known alias, the account goes to the hand-check |
| hq_state, hq_city | Override → Clay → Apollo. A state disagreement goes to the hand-check |
| employees, size_band | Override → Clay → Apollo. A band disagreement goes to the hand-check |
| industry label | Override → Clay's label, if it is on the Industries tab → the label whose NAICS or keywords matched |
| industry_group | Always from the label via the Industries tab, never a vendor's own category |
| naics | Apollo |
| founded_year | Override → Clay → Apollo |
| Funding | The most recent round from Apollo or Clay |
| US headcount, CA/WA/FL share, people leaders | Apollo people (the only source) |

Each fact name has one owning source. `open_roles` comes from `apollo_jobs` only, and `job_posts` supplies posting text only. The default "Hiring and growth" signal reads `apollo_jobs` (open_roles) and `apollo_org` (headcount_growth_12m).

## How the parts stay decoupled

| Layer | Does | Never |
| :- | :- | :- |
| `clients/` | One file per vendor: HTTP calls, response shapes, and the guard | Makes decisions or reads settings |
| `sources/` | One module per source key: vendor → facts in `signal_events` | Scores, calls another source, or writes account columns. The universe module is the only one that creates accounts |
| `clean/` | Pure functions: names, domains, people | Calls a vendor |
| resolver | Facts → account columns, by the table above | Calls a vendor |
| `scoring/` | Facts + sheet → score, tier and angle | Calls a vendor |
| `accounts.py` | The front door: one account per root domain, suppression and partners checked | Calls a vendor |
| `budget.py` | The month and each budget's pace; the week and the weekly target | Calls a vendor |
| `enrol/capacity.py`, `enrol/focus.py`, `limits.py` | The send forecast (from what Instantly last reported), the focus shares, today's number and why | Calls a vendor |
| `contacts/`, `enrol/` | Pick, verify, render, enrol | Change a score |

Jobs talk only through tables and `accounts.status`, never by calling each other. Each one can be re-run safely.

Swapping a vendor means rewriting one client and one source module. The fact names stay the same, so scoring and the sheet don't change. For example, if an in-house reader replaced Clay's page reading, it would write the same `benefit`, `mental_health_provision`, `culture_statement`, `values_page` and `read_status` facts.

## To measure in phase 1

- **Universe size per slice, and so the Apollo credits per sweep.** It has to fit in the month's 2,000 alongside enrichment and reveals.
- **Clay credits per account** (SPEC 8), and so whether 2,000 a month keeps 150 accounts a week ready.
- **How many accounts the free checks keep away from Clay.**
- **Apollo's email hit rate**, which sets how often the Clay Contacts function runs.
- **Instantly's real behaviour on the paused campaigns, before any send:**
  - the step timing, days 0, 7, 14 and 21;
  - the analytics fields;
  - the sending-status codes;
  - whether the API reports the plan's usage.

