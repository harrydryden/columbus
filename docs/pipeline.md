# How an account is found, qualified, cleaned and enriched

Apollo, Clay and Instantly can each find companies, find emails and enrich records, and Apollo, Clay, Instantly and HubSpot each have their own idea of an ICP. This page says which tool does which job, where the ICP lives, and the order in which an account is found, checked and paid for. Items marked **Proposed** change or add to SPEC.md and need Harry's approval (the list is at the end).

## The short answer

- **The sheet is the only ICP.** No persona, saved search, list, score or ICP profile is set up inside any vendor. Every call builds its filters from the sheet when it runs.
- **Each job has one owner.** A second vendor is only ever a fallback, called when the first one misses.
- **Fit finds accounts, and signals rank them.** The main driver is a monthly Apollo organisation search built from the sheet's Industries, States and size bands. Signals never add a company to the universe, with one exception: a company that visits the US site.
- **Free checks come before paid ones.** Clay, the scarcest budget, is spent only on accounts that have passed every free check. An Apollo email reveal is spent only on the contact about to be emailed.
- **Postgres is the only record.** Vendors return observations. Python stores each one as a fact with its source and date, and makes every decision.

## One owner per job

| Job | Owner | Fallback | Not used |
| :- | :- | :- | :- |
| Find companies (the universe) | Apollo organisation search | IRS BMF for nonprofits (January). New site visitors. Great Place To Work and B Corp lists in phase 3 | Clay Find Companies, Instantly SuperSearch, Apollo lists and saved searches |
| Company facts (size, HQ, NAICS, founded) | Apollo | Clay confirms HQ state and size. If they disagree, the account goes to the hand-check | Clay's other data providers; HubSpot auto-enrichment |
| Clean company name | Clay AI, checked against the `clean/names.py` rules | The `clean/names.py` rules alone | |
| Read careers, benefits and values pages | Clay "US Outbound – Accounts" | None: a failed read is scored as "not read" | |
| Funding | Apollo | Clay's Company Latest Funding, run only when Apollo has nothing (**Proposed 2**) | |
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

**1. Find: monthly, `source_universe`.**
- Apollo searches every slice of active industry × active state × size band. Cost: 1 Apollo credit per 100 companies.
- The same front door serves every other way in: new site visitors, IRS BMF matches, and later the GPTW and B Corp lists. Each is matched to an Apollo record by domain.
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

**Proposed 1:** an account also needs at least one candidate contact: a person matching the Roles tab for its size, located in the US outside CA and WA. An account with nobody to email never reaches Clay. **Status: `queued`** once every free check passes, or `disqualified` with the reason.

**3. Verify in Clay: weekdays, `verify_in_clay`, within the Clay budget.**
- It takes `queued` accounts in order of provisional score, enough to keep the verified queue 10 working days deep.
- It runs Apollo organisation enrich (1 credit) first, only if the search record is missing a field Clay needs (**Proposed 6**).
- It then runs the Clay Accounts function in batches of up to 100. The function returns the clean name, confirmed HQ and size, an industry label, the pages it read, benefits, mental-health provision, culture statements, and funding (only when Apollo had none).
- If Clay and Apollo disagree on HQ state or size band, the account goes to the hand-check.
- Afterwards the job-post feeds read the careers URL that Clay found (free).
- It rescores, which sets the final tier and angle. **Status: `verified`.**

**4. Contact: weekdays, `pick_contacts`, just in time.**
- **Proposed 5:** it takes only the accounts the next two enrolment days will use (about twice the daily number), not the whole verified queue.
- It refreshes the candidates with a free people search, picks one by the role rule, and runs Apollo bulk match (verified emails only, Apollo credits).
- On a miss or a catch-all, it runs the Clay Contacts function.
- The email must not be a personal domain, suppressed, or a customer domain. If it fails, it tries the next candidate in fallback order.

**5. Enrol: weekdays at 12:00, `enrol`.**
- It works out today's number, re-checks HubSpot, assigns the test version, renders the copy, and adds the lead to its sender's campaign. **Status: `enrolled`.**

### Why fit, not signals, finds accounts

A search built from signals ("funded in the last 18 months", "hiring a People role") would give a smaller, hotter list. But it would leave out the Control tier: the 15% of sends that go to accounts with no signal. Without the Control tier, the Monday readout can't show what any signal is worth. So the universe is every company that fits, and signals only order it.

The cost of this is the monthly sweep, which is measured in phase 1 (below).

## Which value wins

Sources write facts only. One resolver sets the account's columns from the facts, before every scoring run, by this precedence (**Proposed 3**). Without it, the last job to run would win, so a monthly Apollo sweep would overwrite what Clay had just confirmed.

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

**Proposed 4:** each fact name has one owning source. `open_roles` comes from `apollo_jobs` only, not also from `apollo_org` and `job_posts`. `job_posts` supplies posting text only. The default "Hiring and growth" signal then reads `apollo_jobs` (open_roles) and `apollo_org` (headcount_growth_12m).

## How the parts stay decoupled

| Layer | Does | Never |
| :- | :- | :- |
| `clients/` | One file per vendor: HTTP calls, response shapes, and the guard | Makes decisions or reads settings |
| `sources/` | One module per source key: vendor → facts in `signal_events` | Scores, calls another source, or writes account columns. The universe module is the only one that creates accounts |
| `clean/` | Pure functions: names, domains, people | Calls a vendor |
| resolver | Facts → account columns, by the table above | Calls a vendor |
| `scoring/` | Facts + sheet → score, tier and angle | Calls a vendor |
| `contacts/`, `enrol/` | Pick, verify, render, enrol | Change a score |

Jobs talk only through tables and `accounts.status`, never by calling each other. Each one can be re-run safely.

Swapping a vendor means rewriting one client and one source module. The fact names stay the same, so scoring and the sheet don't change. For example, if an in-house reader replaced Clay's page reading, it would write the same `benefit`, `mental_health_provision`, `culture_statement`, `values_page` and `read_status` facts.

## Proposed changes for Harry

| # | Change | Why | SPEC |
| :- | :- | :- | :- |
| 1 | A free "someone to email" check before Clay: at least one Roles-tab person in the US outside CA and WA | Clay credits are never spent on an account that can't be emailed | 9 (verify_in_clay) |
| 2 | The Clay Accounts function takes `apollo_funding_date` and skips Company Latest Funding when it is set | "Fills funding gaps" then also saves the credits. The function isn't built yet, so this costs nothing to add now | 8 |
| 3 | One resolver with the precedence table above | Stops the last job to run from silently deciding an account's HQ, size or name | 13 (data cleaning) |
| 4 | One owning source per fact (`open_roles` from `apollo_jobs`) | Removes three-way disagreement on the same number | 5, 7 |
| 5 | `pick_contacts` reveals emails only for the next two days' enrolment | No Apollo credits spent on accounts that are later re-ranked or dropped | 9 |
| 6 | Apollo organisation enrich only when the search record lacks a needed field | It may save up to one Apollo credit per verified account; measured in phase 1 | 7 |
| 7 | Say explicitly which vendor features stay off (the tables above). Check, without changing it, whether HubSpot auto-enrichment is on for new companies | Another team's portal setting could spend HubSpot credits and overwrite fields on our warm leads | 1.2, 3 |
| 8 | Decide whether Clay's `catch_all_valid` is sendable. In phase 0, test whether Instantly's "risky contacts off" skips catch-alls | Otherwise a catch-all could be enrolled and then silently never sent | 8, 9 |

## To measure in phase 1

- **Universe size per slice, and so the Apollo credits per monthly sweep.** Apollo's 1,500 a month has to cover:
  - the sweep
  - organisation enrichment for about 800 verified accounts
  - about 650 email reveals

  If the sweep and enrichment together leave too little for reveals, drop enrichment (Proposed 6) or sweep less often.
- **Clay credits per account** (SPEC 8), and how many accounts the free checks keep away from Clay.
- **Apollo's email hit rate**, which sets how often the Clay Contacts function runs.
