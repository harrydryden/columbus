# How an account is found, qualified, cleaned and enriched

Apollo, Clay and Instantly can each find companies, find emails and enrich records, and Apollo, Clay, Instantly and HubSpot each have their own idea of an ICP. This page covers:
- which tool does which job, and where the ICP lives;
- the order in which an account is found, checked and paid for;
- the weekly budgets, and how the system shows which one is holding it back;
- how many emails Instantly can actually send;
- how Harry can steer the system.

Harry approved changes 1–8 on 30 Sep 2026, and set budgets and targets to run weekly (Monday to Sunday, UK time). Changes 9–14 need his answer; the list is at the end.

## The short answer

- **The sheet is the only ICP.** No persona, saved search, list, score or ICP profile is set up inside any vendor. Every call builds its filters from the sheet when it runs.
- **Each job has one owner.** A second vendor is only ever a fallback, called when the first one misses.
- **Fit finds accounts, and signals rank them.** The main driver is an Apollo organisation search built from the sheet's Industries, States and size bands. Signals never add a company to the universe, with one exception: a company that visits the US site.
- **Free checks come before paid ones.** Clay, the scarcest budget, is spent only on accounts that have passed every free check. An Apollo email reveal is spent only on the contact about to be emailed.
- **Budgets are weekly and are spent where they are needed.** Each job that spends credits checks the week's balance before every batch. Enrolment spends none.
- **Postgres is the only record.** Vendors return observations. Python stores each one as a fact with its source and date, and makes every decision.

## One owner per job

| Job | Owner | Fallback | Not used |
| :- | :- | :- | :- |
| Find companies (the universe) | Apollo organisation search | IRS BMF for nonprofits (January). New site visitors. Great Place To Work and B Corp lists (phase 3). Named accounts and lookalikes (**Proposed 12, 13**) | Clay Find Companies, Instantly SuperSearch, Apollo lists and saved searches |
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

**1. Find: weekly, `source_universe`.**
- Apollo searches the slices of active industry × active state × size band. Cost: 1 Apollo credit per 100 companies.
- **Proposed 9:** each week covers a quarter of the slices, so every slice is refreshed every four weeks and the Apollo cost is the same each week, instead of one large monthly bill.
- The same front door serves every other way in: new site visitors, IRS BMF matches, named accounts and lookalikes, and later the GPTW and B Corp lists. Each is matched to an Apollo record by domain.
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

**3. Verify in Clay: weekdays, `verify_in_clay`, within the week's Clay budget.**
- It takes `queued` accounts in order of provisional score, enough to keep the verified queue 10 working days deep, and stops when the week's Clay credits are spent.
- It runs Apollo organisation enrich (1 credit) first, only if the search record is missing a field Clay needs.
- It then runs the Clay Accounts function in batches of up to 100. The function returns the clean name, confirmed HQ and size, an industry label, the pages it read, benefits, mental-health provision, culture statements, and funding (only when Apollo had none).
- If Clay and Apollo disagree on HQ state or size band, the account goes to the hand-check.
- Afterwards the job-post feeds read the careers URL that Clay found (free).
- It rescores, which sets the final tier and angle. **Status: `verified`.**

**4. Contact: weekdays, `pick_contacts`, just in time, within the week's Apollo budget.**
- It takes only the accounts the next two enrolment days will use, not the whole verified queue.
- It refreshes the candidates with a free people search, picks one by the role rule, and runs Apollo bulk match (verified emails only).
- On a miss or a catch-all, it runs the Clay Contacts function.
- The email must not be a personal domain, suppressed, or a customer domain. If it fails, it tries the next candidate in fallback order.

**5. Enrol: weekdays at 12:00, `enrol`.**
- It works out today's number (below), re-checks HubSpot, assigns the test version, renders the copy, and adds the lead to its sender's campaign. **Status: `enrolled`.**

### Why fit, not signals, finds accounts

A search built from signals ("funded in the last 18 months", "hiring a People role") would give a smaller, hotter list. But it would leave out the Control tier: the 15% of sends that go to accounts with no signal. Without the Control tier, the Monday readout can't show what any signal is worth. So the universe is every company that fits, and signals only order it.

## Weekly budgets and targets

A week runs from Monday 00:00 to Sunday 24:00, UK time. Unspent credits don't carry over. The General tab holds:

| Key | Default | Spent or used by | Checked |
| :- | :- | :- | :- |
| `weekly_enrol_cap` | 150 (the spec's 30 a day × 5) | `enrol` | Each send day takes what's left of it ÷ the send days left in the week, so a short Monday is made up by Friday, never in the next week |
| `apollo_weekly_credits` | 500 | `source_universe` (search pages), `verify_in_clay` (enrich), `pick_contacts` (email reveals) | Before every batch. The job stops at zero |
| `clay_weekly_credits` | 0 until Harry sets it (0 means no Clay calls) | `verify_in_clay`, `pick_contacts` (Clay Contacts) | Before every batch. The job stops at zero |
| `apollo_floor` | 5,000 | All Apollo spend | Stops Apollo spend if the account balance falls below it, whatever is left of the week |
| `claude_monthly_cap_usd` | $10 | Reply classification and drafts | Stays monthly. SPEC 1.1 sets it at $10 a month, the same period as the Anthropic Console's own limit |

The budgets are not part of today's enrolment number. The enrol job spends no Clay or Apollo credits, so a budget that has run out never holds back accounts that are already verified and have an email. It limits the stages before enrolment instead. When that leaves too few accounts ready, the limiter says so (below).

**Is 500 Apollo credits a week sustainable?**
- **Balance:** 30,128 credits were left on 29 Sep, and they reset on 21 Aug 2027.
- **Use until the reset:** about 46 weeks × 500 = about 23,000.
- **What that leaves:** about 7,100, which is 2,100 above the floor. That 2,100 is all the headroom for any other Apollo use at Spill.

The 500 a week has to cover three things:
- the weekly sweep: universe ÷ 100 ÷ 4 pages;
- organisation enrichment for each account sent to Clay, about 30 a day;
- email reveals, about 30 a day.

At 150 enrolments a week, reveals and enrichment take up to about 330. The sweep must fit in the rest. Phase 1 measures it.

**Clay:** the week's verified accounts are about `clay_weekly_credits` ÷ credits per account (measured on the first 100 accounts). To keep 150 accounts a week ready, the budget needs roughly 150 × credits per account ÷ the share that passes Clay's checks.

The sheet already has the old monthly and daily rows. Rename them, and settings_sync will reject the General tab until you do:
- `daily_enrol_cap` → `weekly_enrol_cap`, value 150.
- `apollo_monthly_credits` → `apollo_weekly_credits`, value 500.
- `clay_monthly_credits` → `clay_weekly_credits`, value your weekly number.

## Where the limit shows

Today's number is the smallest of three terms:

| Term | What it is | What raises it |
| :- | :- | :- |
| **Weekly target** | What's left of `weekly_enrol_cap` ÷ the send days left this week | A higher `weekly_enrol_cap` |
| **Sending capacity** | Each sender's new leads today, after the follow-ups already due (next section) | Another mailbox, a higher daily cap once warm, or a better cadence (Proposed 10) |
| **Ready accounts** | Verified accounts with a sendable email | Whatever is short behind it (below) |

When ready accounts is the limit, the explanation walks back through the stages and names the first one that is short:
1. verified accounts still waiting for an email, and whether Apollo's weekly budget is spent;
2. Clay has no weekly budget;
3. Clay's weekly budget is spent while accounts wait;
4. accounts are waiting for Clay;
5. nothing is waiting for Clay, so the universe or the free checks are the limit.

The same picture appears in three places now, and two more with Slack:
- **The enrol job's summary** (`limited_by` and `limits`), kept in its heartbeat row and in the Railway log.
- **`us-outbound status`**, under "This week". For example:

  ```
  This week (Monday to Sunday, UK time):
    Today: 18, limited by ready accounts (weekly target 38, sending capacity 31, ready accounts 18).
    Weekly target: 0 of 150 enrolled this week (Monday to Sunday, UK time), 4 send days left.
    Sending capacity, Hannah Spalding: 8 new leads today, 30 sends a day (a new lead sends 4 emails, so 8 new a day keeps 30 sends a day steady).
    Clay: 600 of 600 credits used this week, 0 left.
    Apollo: 212 of 500 credits used this week, 288 left.
    Behind it: Clay's weekly budget is used and 41 accounts are waiting to be verified until Monday.
  ```
- **`v_budgets`** in Postgres: each budget's spend and balance this week (Claude: this month).
- **The daily post** (phase 3, Slack): the `limited_by` line, every morning. Until Slack is set up, it goes to the log.
- **The Monday readout** (phase 3): how many days each term was the limit last week, and each budget's use (**Proposed 11**, with alerts when a budget is 80% spent before Thursday, or when the same limit binds three send days running).

## How many emails Instantly can send

Instantly decides the moment each email goes. The jobs decide how many new leads go in, and check the limits Instantly works within:

| Limit | Set by | What the jobs do |
| :- | :- | :- |
| Each mailbox's daily cap | The Mailboxes tab (`daily_cap`, 30) and Instantly's own limit on the account | mailbox_health reads Instantly's limit every morning. The forecast uses the lower of the two and says when Instantly's is lower |
| The campaign's daily limit | The jobs set it to the sum of the owner's Active caps | mailbox_health checks it for drift every day and fixes it with `campaigns ensure --fix` |
| The send window | Mon–Fri 09:00–16:00 ET, set by the jobs | No sends at weekends or on blackout dates |
| Step timing | The campaign's steps: day 0, 3, 8, 15 | **Instantly counts delays in calendar days.** A step due on a Saturday or Sunday goes on Monday, and the next step waits from Monday |
| Warmup | Instantly | Warmup emails are separate from the campaign limit, and warmup stays on (SPEC 13) |

**The forecast** (`enrol/capacity.py`, which replaces the spec's "caps ÷ 4") works per sender, because each account keeps its sender for life:
- Every enrolled lead holds a slot on the days its later steps will go out. Leads that replied, bounced or unsubscribed hold nothing.
- A new lead sends four emails, so a sender takes at most **capacity ÷ 4** new leads a day. That pace keeps a full day steady. Filling every free slot at once would crowd the days those leads' follow-ups land on, and leave later days idle.
- It takes fewer when, on any of the four days a lead enrolled today would send, the follow-ups already due leave less room.
- So no email of any lead, old or new, ever has to wait for a full inbox.
- New accounts go to the sender with the largest share of today's pace left, so Harry's two mailboxes take twice Hannah's or Sam's share.

**What the forecast found (Proposed 10, [ASK HARRY]).** With steps on days 0, 3, 8 and 15, leads enrolled Wednesday to Friday have most of their later steps fall at a weekend and move to Monday. Mondays fill first and hold the whole week back. Simulating 12 weeks with the four inboxes (120 sends a day):

| Cadence | New accounts a week | Sequence length |
| :- | :- | :- |
| Days 0, 3, 8, 15 (SPEC 10) | about 61 | 15 days |
| Days 0, 2, 7, 14 | about 100 | 14 days |
| **Days 0, 7, 14, 21** | **150**: every inbox full every day | 21 days |
| Days 0, 7, 14 (three steps) | 200 | 14 days |

The spec's "about 650 a month" assumed every day could be filled evenly. With Instantly counting calendar days, the current cadence gives about 260 a month.

A weekly cadence puts every step on the same weekday as the first email, so no step ever lands on a weekend. It reaches the 150 target with today's inboxes, and also avoids Monday spikes, which hurt deliverability. The copy doesn't change, only the gaps. The cadence is set in one place (`clients/instantly.py`, `STEP_DAYS`). The campaigns and the forecast both read it, so they can't drift apart.

**Not yet counted, and when they will be:**
- **Instantly's backlog** (steps due but not sent, for example after an error): sync_outcomes (phase 2) reads it from Instantly, and it comes off capacity.
- **The plan's own caps** (emails a month and uploaded contacts, shared with the EU campaigns in the same workspace): still an open phase 0 fact. **Proposed 14:** once they're known, a General key `instantly_weekly_emails` for this system's share, checked as a fourth term.
- **Kill rules** (phase 3): a mailbox paused for bounces or blocks drops out of capacity the same day.

## How Harry can steer the system

**In the sheet already (no code):**

| Lever | Tab | Effect |
| :- | :- | :- |
| Turn an industry on or off | Industries: `active` | The universe search and the queue include it or leave it out |
| Put an industry first | Industries: `priority` | Its accounts go ahead of others at the same score and size |
| Widen or narrow an industry | Industries: `naics_prefixes`, `exclude_naics`, `apollo_keywords` | Changes the Apollo filters and the Python re-check |
| Where | States | HQ states in or out (CA and WA never) |
| Who | Roles | Titles and who comes first by company size |
| What counts | Signals | Weights, new keyword signals on an existing source, Hold and Exclude rules |
| A single company | Overrides | Any field for one domain, winning over every source |
| Mix | General: `priority_threshold`, `standard_threshold`, `control_share` | How tiers are cut, and the share kept for the control group |

**Proposed (need a small build each):**
- **12. Industry weekly share.** A `weekly_share` column on Industries, for example Technology & Startups 60% and Marketing & Creative Agencies 40%. Enrolment fills each group's share of the week, and a share that can't be filled passes to the others. This steers volume directly, not just order.
- **12b. Named accounts.** A tab where Harry lists domains he wants approached. They enter by the same front door and pass the same checks, including Clay and suppression. An optional Signals row, "Named by Harry", lifts their score. They are tagged, so the readout can report them separately.
- **13. Lookalikes.** A Seeds tab of companies Harry wants more of (for example Spill's best customers; these could also be read from HubSpot's closed-won companies, read only). The system reads each seed's Apollo record, one enrichment credit per seed, once. Then:
  - **Discovery:** extra Apollo searches built from the seeds' most common Apollo keyword tags, within the active states and sizes. New companies still enter by the front door.
  - **Ranking:** every account gets a `lookalike_similarity` fact from 0 to 1, from shared keyword tags, NAICS prefix and size band. A Signals row ("Looks like our customers", `lookalike_similarity >= 0.5`, weight +15) scores it like any other signal, and the readout shows what it's worth.

  This costs nothing beyond the seed lookups. Clay and Apollo have their own lookalike tools. Clay's charges per result through its data providers, and Apollo's would put the ICP inside Apollo, so neither is used.

## Changes for Harry

| # | Change | Why | SPEC | Status |
| :- | :- | :- | :- | :- |
| 1 | A free "someone to email" check before Clay | Clay credits are never spent on an account that can't be emailed | 9 | Approved 30 Sep |
| 2 | The Clay Accounts function takes `apollo_funding_date` and skips Company Latest Funding when it is set | Saves Clay credits | 8 | Approved |
| 3 | One resolver with the precedence table (below) | Stops the last job to run from deciding an account's HQ, size or name | 13 | Approved |
| 4 | One owning source per fact (`open_roles` from `apollo_jobs`) | Removes three-way disagreement | 5, 7 | Approved |
| 5 | `pick_contacts` reveals emails only for the next two days | No credits spent on accounts later re-ranked or dropped | 9 | Approved |
| 6 | Apollo organisation enrich only when needed | Up to one Apollo credit saved per verified account | 7 | Approved |
| 7 | Vendor features stay off; check HubSpot auto-enrichment without changing it | Keeps one record and one ICP | 1.2, 3 | Approved |
| 8 | Decide whether `catch_all_valid` is sendable; test Instantly's "risky contacts off" | Otherwise a catch-all could be enrolled and never sent | 8, 9 | Approved; the test is in phase 0 |
| — | Weekly budgets and targets | Harry's instruction | 5, 9 | Done |
| — | The send forecast replaces "caps ÷ 4" | Accounts for follow-ups and Instantly's own limits | 9 | Done |
| 9 | A weekly universe sweep over a quarter of the slices | Even Apollo spend each week | 9 | Proposed |
| 10 | **[ASK HARRY]** Steps on days 0, 7, 14, 21 instead of 0, 3, 8, 15 | 150 a week instead of about 61 with today's inboxes; no Monday spikes | 10 | Proposed |
| 11 | Limit and budget lines in the Monday readout, with the two alerts | Shows the bottleneck without asking | 12 | Proposed |
| 12 | Industry weekly share, and a Named accounts tab | Steer volume and specific companies from the sheet | 5 | Proposed |
| 13 | A Seeds tab for lookalikes | More companies like the best ones, at no extra cost | 5, 7 | Proposed |
| 14 | `instantly_weekly_emails` once the plan's caps are known | The workspace is shared with the EU campaigns | 1.2, 9 | Proposed |

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
| `budget.py` | The week, each budget's balance, the weekly target | Calls a vendor |
| `enrol/capacity.py`, `limits.py` | The send forecast, today's number and why | Calls a vendor |
| `contacts/`, `enrol/` | Pick, verify, render, enrol | Change a score |

Jobs talk only through tables and `accounts.status`, never by calling each other. Each one can be re-run safely.

Swapping a vendor means rewriting one client and one source module. The fact names stay the same, so scoring and the sheet don't change. For example, if an in-house reader replaced Clay's page reading, it would write the same `benefit`, `mental_health_provision`, `culture_statement`, `values_page` and `read_status` facts.

## To measure in phase 1

- **Universe size per slice, and so the Apollo credits per weekly sweep.** It has to fit in the 500 a week alongside enrichment and reveals.
- **Clay credits per account** (SPEC 8), and so the weekly Clay budget that keeps 150 accounts a week ready.
- **How many accounts the free checks keep away from Clay.**
- **Apollo's email hit rate**, which sets how often the Clay Contacts function runs.
- **Instantly's real step timing**, to confirm the calendar-day delays and the Monday pile-up on the three paused campaigns before any send.
