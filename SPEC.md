# US Outbound: build spec
Owner: Harry Dryden (<harry@spill.chat>), Spill. Version 1, 29 Sep 2026.
This is the complete brief. Build from it. If something is missing or ambiguous, stop and ask Harry rather than guess.
-----
## 0. How to work
1.  Read the whole file before writing code.
2.  Build in the phase order in section 14. Each phase ends with acceptance checks. Show Harry the result and wait for his go-ahead before the next phase.
3.  Every job runs in **dry-run by default**. In dry-run it computes, logs and writes to BigQuery, but sends nothing. It writes nothing to HubSpot or Instantly, and nothing to Slack except #us-outbound-dev. Live behaviour needs both the --live flag and the setting live_sending = yes.
4.  Checkpoints marked **[ASK HARRY]** are hard stops.
5.  Keep the code small and plain: Python 3.12, no framework unless it removes real code. Settings, and what each signal means, live in the settings sheet, not in code.
## 1. Guardrails (non-negotiable)
1.  **No new paid services or plan upgrades.** The only new cost allowed is the Claude API key created for this system, capped at $10 a month. If any other step needs spend, stop and ask.
2.  **Isolation from existing work.**
  - **Clay:** work only inside the folder "US Outbound". Call the existing workspace functions as they are and never modify them. Anything borrowed is duplicated into the folder first.
  - **HubSpot:** write only:
  - the six `us_outbound_*` properties
  - notes and tasks
  - deals in the "Spill 3.0" pipeline
  - hubspot_owner_id, lifecyclestage and hs_lead_status, and those three only when they are empty
Never create a record for a contact who has not replied positively or referred us. Never edit workflows, sequences, lists or properties owned by others. The Apollo–HubSpot native sync stays off.
  - **Instantly:** the workspace is shared with other (European) campaigns. Touch only the campaigns named "US Outbound – {sender}" (one per mailbox owner, section 9) and the accounts in the mailbox registry.
  - Every API call that lists or reads emails, leads or accounts must filter by registry account ids.
  - Log every account id read.
  - Never change workspace-level settings or other campaigns.
  - **BigQuery:** write only to dataset us_outbound.
  - **Apollo:** read only. Save no lists, labels or sequences.
1.  **Human in the loop.** Nothing is sent to a prospect after their first reply unless Harry has approved it in Slack. Any human-in-the-loop item left for 24 hours is forwarded by email to <harry@spill.chat> (section 11). Nobody replies to a prospect from <harry@spill.chat>: replies always go from the account's sender mailbox.
2.  **spill.chat never sends cold email.** Only the registered mailboxes send.
3.  **Recipients.** Never email a contact located in California or Washington, whatever the company HQ. Never email a personal-domain address (gmail.com, outlook.com, yahoo.com and similar).
4.  **Credits.** Never exceed the monthly Clay and Apollo budgets in settings. Check the balance before every batch.
5.  **Secrets.**
      - All keys live in Google Secret Manager, never in the repo, logs or BigQuery.
      - Logs carry hashed emails and at most 200 characters of any email body.
6.  **Tracking.**
      - Add no scripts or pixels to employee-facing pages.
      - Apollo contact-level visitor tracking stays off.
      - Open and link tracking stay off in Instantly.
7.  **Copy rules** (section 10) are enforced by a check at render time that blocks the send, not only by review.
## 2. What the system does
An account-based outbound engine for US organisations of 10 to 249 employees. It runs from a few settings (industries, states, company size and a sheet of signals) to booked demos, with under three hours of human time a week.
|  |  |  |
| :- | :- | :- |
| **#** | **Job** | **Done looks like** |
| 1 | Find target companies using market signals | A two-week queue of accounts that Clay has verified and the signal sheet has scored and tiered |
| 2 | Find the right person | One verified contact per account by the role rule; US-located; not in CA or WA |
| 3 | Run a personalised outbound motion | About 30 new accounts per working day into evergreen Instantly campaigns, one per sender; four steps over 15 days; each account hears from one sender throughout, and every demo is booked with Harry |
| 4 | Respond and hand off | Every reply classified within 20 minutes; positive replies alerted in Slack with a draft; Harry approves and the jobs send it from the account's sender; anything left for 24 hours is forwarded to <harry@spill.chat> |
| 5 | Manage warm leads in HubSpot | Company and contact created only on a positive or referral reply; one deal per company, only when a demo is requested or booked |
| 6 | Clean data | Clean company names, one account per root domain, normalised people fields, before anything is stored or sent |
| 7 | Suppress | One hashed suppression list; kill rules on bounces, blocks and unanswered replies; re-contact limits |
| 8 | Learn | One A/B test at a time; a Monday readout of what each signal and copy version is worth; Harry adjusts the sheet |
| 9 | Scale within limits | Daily enrolment is the smallest of mailbox capacity, Clay budget, Apollo budget and the cap |
| 10 | Comply | CAN-SPAM footer and opt-out; UK GDPR notice; Apollo deletion notices honoured within 30 days |
| 11 | Measure | UTMs, an events table, meetings and deals read back, cost per meeting by industry group |
| 12 | Operate safely | One owner (Harry) with email escalation, heartbeats, dry-run, holiday calendar, kill switch, stop rule |

**Volume.** Starting capacity is about 650 new accounts a month (4 mailboxes × 30 sends × 21.7 working days ÷ 4 steps). Adding mailboxes to the registry raises it; nothing else needs to change.
**Target.** v1 complete by 18 Dec 2026.
The system is built on seven design principles:
  - The company is the unit: it has one score, one tier and one contact in v1, and a reply from anyone stops outreach to the whole account.
  - Each account has one sender for its whole life: every step, every reply and any later re-contact come from the same person and mailbox. Every demo is booked with Harry.
  - Every account passes through Clay before it can be emailed.
  - Only the Python jobs write to HubSpot and Instantly.
  - HubSpot holds warm leads only.
  - There is no public endpoint: everything polls.
  - One A/B test runs at a time, and nothing re-weights itself.
  - Names everywhere are plain English: website industry labels, and role, angle and tier names.
## 3. Architecture
```
flowchart LR
  subgraph Sources
    AP[Apollo: universe, people, signals, site visits]
    PUB[Public: IRS BMF, careers-site feeds, layoffs/WARN, calendar]
  end
  SHEET[Google Sheet: US Outbound – Settings] -->|nightly sync| BQ
  AP --> JOBS
  PUB --> JOBS
  JOBS[Python jobs on Cloud Run] <--> BQ[(BigQuery us_outbound)]
  JOBS <-->|function calls| CLAY[Clay: US Outbound folder]
  JOBS -->|enrol leads| INST[Instantly: one 'US Outbound' campaign per sender, registry mailboxes]
  INST -->|emails API, polled| JOBS
  JOBS -->|warm leads only| HS[HubSpot]
  HS -->|meetings and deals, polled| JOBS
  JOBS <-->|alerts, approvals, posts| SLACK[Slack #us-outbound]
  JOBS -->|classify, draft| CLAUDE[Claude API]
```
|  |  |  |
| :- | :- | :- |
| **Component** | **Role** | **Must never** |
| Python jobs (Cloud Run Jobs + Cloud Scheduler, in Spill's existing Google Cloud project) | All orchestration: sourcing, Clay calls, scoring, contact choice, enrolment, polling, classification, HubSpot and Slack writes, readouts, kill rules | Expose a public endpoint; send email itself |
| BigQuery us_outbound | System of record | Hold secrets |
| Google Sheet "US Outbound – Settings" | Everything Harry edits: general settings, signals, angles, industries, states, roles, copy, mailboxes, overrides, tests | Hold prospect data |
| Apollo | Universe (organisation and people search), verified email reveal, organisation fields, job titles, company-level site visits | Be written to |
| Clay | Fidelity layer: cleans names; confirms company facts; reads careers and benefits pages; fills funding gaps; runs the email waterfall where Apollo misses | Write to HubSpot or Instantly; hold signal weights |
| Instantly | Sending, warmup, blocklist, reply endpoint | Hold routing or copy logic (the Unibox is not used) |
| HubSpot | Warm leads only | Hold cold contacts |
| Slack | Alerts, approvals, daily post, Monday readout |  |
| Claude API | Reply classification, draft replies, readout observations | Send anything without approval |

## 4. Existing systems and identifiers
|  |  |
| :- | :- |
| **System** | **Identifier / state** |
| HubSpot | Portal 8481055. Deals go in the pipeline "Spill 3.0", first stage. Look up the pipeline and stage ids through the API and store them in settings. |
| Clay | Workspace "Spill", id 1336346. Existing functions to call, not modify: Work Email t_0tk0v4lhJ895hhhhTHJ, Company Latest Funding t_0tk0v4ehpQ6WaeuCoQf, Website Technology Stack t_0tk0v4lEmZDggj3wRwB, Website Traffic t_0tk0v4lJQkpANSDTxf9 |
| Apollo | Team 6a85cc72550d280018aa9e9f. 30,078 credits on 29 Sep 2026. Waterfall email enabled. |
| Apollo website visitor tracker | Tracker 6a85cc77f43ea3001cfcd35b, on domain spill.chat (referrer 6abbec3465dc3e001cc120a4). Intent paths: "/us" (high) and "us/pricing" (high; must be corrected to "/us/pricing"). Contact-level tracking is off. No data had been received on 29 Sep. |
| Instantly | Existing workspace, shared with other campaigns. US mailboxes are listed below. |
| US mailboxes (starting registry) | <hannah@meetspill.org> (Hannah Spalding), <sam@meetspill.org> (Sam Jackson), <harry@meetspill.org> and <harry@tryspill.org> (Harry Dryden). Hannah and Sam have agreed to their addresses being used. |
| Webflow US locale | 6a7b1a45465079bd9e1fe40f. Landing pages: EAP 697b92cc0cfee77bab89a0c1, Mental Health Benefit 6aa12c86735034f9178d331b, Comparison 6aa12cffc18d48ca12dfb35e, On-demand 6a919400ce9593ca2dc11ea0 |
| Demo booking (always with Harry) | In emails and replies: <https://meetings.hubspot.com/harry336/us-demo-link>. On the website: <https://www.spill.chat/us/book-demo>. Both book into Harry's HubSpot calendar. |
| US prices (Core plan, flat monthly by team size) | 1–10: $195 · 11–25: $250 · 26–50: $350 · 51–100: $495 · 101–200: $995 · 201+: $5 per employee |

Start afresh. The engine sources its own accounts and does not import any earlier outreach list. Contacts already in HubSpot are covered by the normal exclusion rules (section 9).
## 5. Settings sheet: "US Outbound – Settings"
A job syncs every tab into BigQuery settings, nightly and on demand.
  - It validates every row first. A tab that fails validation keeps yesterday's valid version in force, and the errors are posted to Slack.
  - Every version is stored with effective_from and effective_to.
  - After each sync the queue is rescored, so a change made today shapes tomorrow's enrolment.
### Tabs
**General** (key, value). Defaults:
|  |  |
| :- | :- |
| **Key** | **Default** |
| live_sending | no |
| daily_enrol_cap | 30 |
| control_share | 0.15 |
| priority_threshold | 50 |
| standard_threshold | 20 |
| score_cap | 100 |
| clay_monthly_credits | a quarter of the Clay pool, until credits per account are measured |
| apollo_monthly_credits | 1500 |
| apollo_floor | 5000 |
| approver_slack_ids | Harry (the only approver) |
| escalation_email | <harry@spill.chat> |
| escalation_hours | 24 |
| alert_channel | #us-outbound |
| booking_link | <https://meetings.hubspot.com/harry336/us-demo-link> |
| booking_page | <https://www.spill.chat/us/book-demo> |
| demo_host | Harry Dryden |
| postal_address | Spill's UK registered address |
| privacy_url | the US privacy and opt-out page |
| send_window | Mon–Fri 09:00–16:00 America/New_York |
| blackout_dates | 2026-11-23..2026-11-27, 2026-12-18..2027-01-04 |
| recontact_person_months | 12 |
| recontact_account_months | 6 |
| stop_rule_accounts | 1500 |
| stop_rule_meetings | 5 |
| hubspot_pipeline | Spill 3.0 |
| hubspot_deal_stage | the first stage of Spill 3.0 |
| claude_model | a current fast Claude model |
| claude_monthly_cap_usd | 10 |

**Signals.** Columns:
|  |  |  |
| :- | :- | :- |
| **Column** | **Type** | **Notes** |
| signal | text, unique | Plain name |
| source | enum | One of the source keys in section 7 |
| looks_for | text | For text sources: terms separated by semicolons, matched case-insensitively as whole words. For field sources: a condition (syntax below) |
| context_rule | text, optional | Terms that must appear near a match, for ambiguous brand names. "Headspace" needs "for Work", "app" or "subscription"; "Calm" needs "app", "premium", "business" or "subscription" |
| weight | integer | Negative allowed |
| max_weight | integer, optional | Cap for signals that can match more than once |
| action | enum | Score, Hold, Exclude, Suppress |
| suggests_angle | text, optional | Must exist on the Angles tab |
| opener | text, optional | Opener template, e.g. Saw your benefits page mentions {evidence} |
| counts_for_days | integer | Freshness window |
| active | yes/no |  |
| note | text | Why the row is set this way, who changed it, when |

Condition syntax for field sources:
  - Conditions take the form field op value, joined by AND or OR.
  - The operators are = != > >= < <= in contains.
  - A value is a number, a quoted string or a [list].
  - Parse conditions with a small hand-written parser. Never eval.
**Angles** (angle, order, argument, default_opener, landing_page_override, active). When an account's signals suggest several angles, the order decides which one it gets.
**Industries** (industry, industry_group, active, naics_prefixes, exclude_naics, apollo_keywords, landing_page_url, proof_point, priority).
**States** (state, active, note).
**Roles** (role, titles, first_choice_for_size, fallback_order).
**Copy** (copy_version, angle, step, subject, body, status, approved_by, sources). Status is draft, approved or retired. Only rows with status approved are ever sent.
**Mailboxes**:
|  |  |
| :- | :- |
| **Column** | **Notes** |
| address |  |
| instantly_account_id |  |
| domain |  |
| provider |  |
| owner_name |  |
| owner_role |  |
| signature |  |
| status | Warming, Active, Paused or Retired |
| daily_cap |  |
| added_on |  |
| retire_after |  |

**Overrides** (domain, field, value, note). An override wins over every source.
**Tests** (test_id, hypothesis, version_a, version_b, accounts_per_version, start_date, read_date, decision_rule, status, result).
### Default content to load
**Mailboxes**:
|  |  |  |  |  |
| :- | :- | :- | :- | :- |
| **address** | **domain** | **owner_name** | **status** | **daily_cap** |
| <hannah@meetspill.org> | meetspill.org | Hannah Spalding | Active if already warm, otherwise Warming | 30 |
| <harry@meetspill.org> | meetspill.org | Harry Dryden | as above | 30 |
| <sam@meetspill.org> | meetspill.org | Sam Jackson | as above | 30 |
| <harry@tryspill.org> | tryspill.org | Harry Dryden | as above | 30 |

For each mailbox:
  - Read its warmup status from Instantly and set the status from it.
  - owner_role is optional. The default signature is the owner's full name, "Spill" and spill.chat/us; Harry can add roles in the sheet.
  - The owner is the real person named on the address. Replies are drafted and signed in the owner's name.
**Signals** (every one editable):
|  |  |  |  |  |  |  |
| :- | :- | :- | :- | :- | :- | :- |
| **Signal** | **Source** | **Looks for** | **Weight** | **Action** | **Suggests angle** | **Counts for (days)** |
| Mental health support listed | clay_careers | mental health; EAP; employee assistance; therapy; counseling; counselling; wellbeing support; well-being support | +25 | Score | Progressive employer | 540 |
| EAP named | clay_careers | EAP; employee assistance; ComPsych; GuidanceResources; Magellan | +10 | Score | Upgrade the EAP | 540 |
| Modern mental-health vendor named | clay_careers, job_posts | Talkspace; Lyra; Modern Health; Spring Health; Headspace; Calm; BetterUp; Nivati; Tava; Wellhub; Gympass; Wellbound; Justworks Plus (with context rules) | 0 | Hold | Switch from a competitor | 540 |
| Progressive benefits | clay_careers, job_posts | wellness stipend; mental health day; unlimited PTO; four-day week; 4-day week; parental leave; sabbatical; 100% employer-paid | +10 each, max +30 | Score | Progressive employer | 540 |
| Culture or values page | clay_careers | values_page = true | +10 | Score | Progressive employer | 540 |
| People leader in place | apollo_people | people_leader_count >= 1 | +10 | Score |  | 365 |
| New People leader | apollo_people | people_leader_days_in_title <= 90 | +30 | Score | Progressive employer | 90 |
| First People hire | apollo_jobs | open_people_roles >= 1 AND people_leader_count = 0 | +25 | Score | Growing team | 90 |
| Recent funding | apollo_org, clay_funding | days_since_funding <= 540 | +20 | Score | Growing team | 540 |
| Hiring and growth | apollo_org | open_roles >= 3 OR headcount_growth_12m >= 0.10 | +15 | Score | Growing team | 90 |
| Visited the US site | site_visits | us_visits_30d >= 1 | +20 | Score |  | 30 |
| Viewed US pricing or demo page | site_visits | pricing_or_demo_visits_30d >= 1 | +15 | Score |  | 30 |
| Nonprofit budget | irs_bmf | revenue >= 2000000 AND revenue <= 50000000 | +15 | Score |  | 400 |
| Nonprofit fiscal year ahead | irs_bmf | days_to_fiscal_year_start >= 60 AND days_to_fiscal_year_start <= 120 | +20 | Score |  | 1 (recomputed daily) |
| Q4 plan-year window | calendar | month in [10, 11, 12] | +10 | Score |  | 1 (recomputed daily) |
| Layoffs | layoffs | days_since_layoff <= 90 |  | Suppress |  | 90 |

An EAP or other listed mental-health support is a positive sign: wellbeing is part of that employer's brand. It is never an exclusion.
**Angles**, in order:
1.  **Upgrade the EAP.** "Your team already has an EAP, which says you take this seriously. This is the version they'll use: same-day counseling in Slack or Teams, and 30% of employees use Spill."
2.  **Progressive employer.** "You already invest in your people. This is the benefit they'll actually use."
3.  **Growing team.** "Hiring fast means onboarding stress. Keep the people you just hired, for a flat fee from $195 a month."
4.  **General.** "Mental health support your team will use: same-day counseling for one flat monthly fee." Control-tier accounts always get this angle.
5.  **Switch from a competitor** (inactive in v1). "About a third of the price, 50-minute sessions rather than 30, and no billable maximums that run out in August."
The Legal Teams industry group also gets an overlay line: "The bar's Lawyer Assistance Program covers attorneys. Who covers paralegals and staff?"
**Industries.** These are the website's labels, grouped under its 15 industry pages.
|  |  |  |  |
| :- | :- | :- | :- |
| **Industry group** | **Industries (labels)** | **Find with (Apollo)** | **v1** |
| Technology & Startups | Technology & Startups, Startups, Fintech, Healthtech, Insurtech, Edtech, Proptech, Legaltech, Adtech & martech, Digital health, AI & deep tech, Cybersecurity, Agritech, Gametech, Cleantech, Traveltech, Games studios | NAICS 5112, 513210, 5415, 518210, plus a keyword per label | On from 26 Oct |
| Marketing & Creative Agencies | Marketing & Creative Agencies, Marketing agencies, Advertising agencies, Creative & design agencies, PR agencies, Content agencies, UX & product design agencies, Events & experiential agencies, Production studios, Publishers | NAICS 5418, 541430, 541613, 5121, 5111 | On from 26 Oct |
| Nonprofits | Nonprofits, Human rights, Disability organizations, Animal welfare, Social welfare, Youth development, Community development, International aid & relief, Environmental nonprofits, Health & medical nonprofits, Arts & culture, Churches & religious organizations, Emergency & rescue | IRS Business Master File 501(c)(3) by NTEE, matched to Apollo; NAICS 813, 624 | January; churches and emergency services off |
| Legal Teams | Legal Teams | NAICS 541110 | January |
| Professional Services | Professional Services, CPA firms, Management consulting, Architecture studios, Engineering & design firms, Research & market intelligence, Staffing agencies, HR consulting | NAICS 5412 (not 541214), 5416 (not 541612), 54131, 54133, 5419 | After January; Staffing agencies off; HR consulting never |
| Financial Services, Healthcare, Senior Care & Home Care, Education, Hospitality, Retail & E-commerce, Construction & Trades, Manufacturing & Industrial, Fitness & Recreation | All their website labels |  | Off |
| Small Businesses; Remote & hybrid teams |  | Not industries: a size band, and a flag for staff in 3 or more states |  |

**States.**
  - Active: NY, MA, NJ, PA, IL, GA, TX.
  - FL: off until Harry confirms.
  - Wave 2: NC, VA, MD, OH, MN.
  - CA and WA: never.
**Roles.**
  - **Titles:**
      - People leader: Head of People; VP People; Chief People Officer; People Ops Lead; HR Director; Director of HR; HR Manager; People & Culture.
      - Founder or executive: CEO; Founder; Co-founder; President; Managing Partner; Managing Director; Executive Director.
      - Operations: COO; Chief of Staff; Head of Operations; Director of Operations; Office Manager; Firm Administrator.
  - **Who to contact first:**
      - Companies with 10 to 49 staff: Founder or executive first, then Operations.
      - Companies with 50 to 249 staff: People leader first, then Operations, then Founder or executive.
      - Finance is not contacted.
  - **Skip:** interns, coordinators, recruiters, sales ops and CS ops, any EMEA or APAC title, and generic mailboxes (info@, hr@).
## 6. BigQuery dataset us_outbound
Use the same location as the project's existing datasets and a dedicated service account. The DDL lives in sql/ddl/.
|  |  |  |
| :- | :- | :- |
| **Table** | **One row per** | **Columns** |
| accounts | Company (root domain) | account_id (uuid), domain, clean_name, legal_name, apollo_org_id, hq_city, hq_state, industry, industry_group, naics, employees, us_employees, size_band, founded_year, source (apollo / irs / site_visit), score, tier (Priority / Standard / Control / Held / Excluded), tier_reason, angle, sender (owner name; set at first enrolment, never changed), status (new / queued / verified / enrolled / engaged / demo_requested / demo_booked / disqualified), clay_checked_at, clay_credits_used, hubspot_company_id, first_seen, last_scored |
| contacts | Person | contact_id, account_id, role, title, first_name, last_name, email, email_sha256, email_status, email_source (apollo / clay), person_state, enrolment_month, angle, copy_version, test_id, mailbox (the sender's address that sent step 1), instantly_campaign, instantly_lead_id, hubspot_contact_id, suppressed, suppressed_reason, created_at |
| signal_events | Observation with evidence | event_id, account_id, source, fact, value, quote (≤300 chars), source_url, observed_at |
| events | Send, reply, visit, meeting, deal or escalation | event_id (idempotency key: Instantly email id or HubSpot object id), contact_id, account_id, type (sent / bounced / replied / unsubscribed / site_visit / meeting_booked / demo_held / deal_created / escalated), step, mailbox, reply_class, reply_text (purged after 90 days), language_terms, competitor_named, approval (approved / edited / skipped), approved_by, occurred_at |
| suppression | Do-not-contact entry | email_sha256, domain, reason, source, added_at; kept indefinitely |
| settings | Setting version | tab, key, values (JSON), effective_from, effective_to, synced_at |
| Raw loads | As received | raw_irs_bmf, raw_job_posts, raw_clay_accounts, raw_clay_contacts, raw_site_visits, raw_layoffs |

Views: v_queue, v_signal_value, v_readout_weekly, v_mailbox_health, v_credits_month, v_heartbeats.
**Retention and erasure.**
|  |  |
| :- | :- |
| **Data** | **Rule** |
| Universe rows not refreshed in 12 months | Deleted |
| Contacts who never replied | Deleted 12 months after their last step |
| Reply text | Purged after 90 days |
| Suppression | Kept indefinitely, as hashes |

The command erase --email handles an erasure request across BigQuery, HubSpot, Instantly and Clay within 30 days.
## 7. Sources
Each source module writes facts {account_id, source, fact, value, quote, url, observed_at} into signal_events. The scoring module matches facts to the Signals tab. Adding a keyword signal on an existing source needs no code; adding a new source needs a new module.
|  |  |  |  |  |
| :- | :- | :- | :- | :- |
| **Source key** | **Provides** | **How** | **When** | **Cost** |
| apollo_org | Firmographics, NAICS, employees, HQ, open_roles, headcount_growth_12m, funding | Organisation search, sliced by state × industry × size band to stay under the 50k results cap; enrichment | Monthly (universe); at queue entry | 1 credit per page of 100; 1 per enrichment |
| apollo_people | People leader present, days in title, US headcount by state | People API search with both person_locations and organization_locations set | Weekly | 0 credits |
| apollo_jobs | open_people_roles, open_roles | Organisation search filters q_organization_job_titles, organization_num_jobs_range | Weekly | 0–1 credits |
| site_visits | us_visits_30d, pricing_or_demo_visits_30d, top_paths, first_visit_at | Apollo website visitors on tracked domain spill.chat, paths under /us | Daily | Expected free; confirm in phase 0 |
| clay_careers | Benefits, mental-health provision, culture, values_page (with quotes and URLs) | Clay function "US Outbound – Accounts" (section 8) | At queue entry; re-read after 180 days | Clay credits |
| clay_funding | Funding where Apollo has none | Inside the same Clay function, which wraps Company Latest Funding | At queue entry | Clay credits |
| job_posts | Posting text for benefits and vendor names | Careers URL found by Clay, then the public feeds: Greenhouse boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true, Lever api.lever.co/v0/postings/{slug}?mode=json, Ashby api.ashbyhq.com/posting-api/job-board/{slug}, Workable apply.workable.com/api/v1/widget/accounts/{slug} | Weekly, queued accounts | Free |
| irs_bmf | 501(c)(3) status, NTEE, revenue, fiscal year-end month | IRS Exempt Organizations Business Master File CSVs by state, matched to Apollo on website, or name plus ZIP | Monthly | Free |
| layoffs | days_since_layoff | layoffs.fyi and the Big Local News warn-scraper output | Weekly | Free |
| calendar | month, days_to_fiscal_year_start | Computed | Daily | Free |

**Site visits.**
  - **Discovery.** In phase 0, find out whether Apollo exposes the list of companies visiting a tracked site through its API (for example, a website-visitor filter on organisation or people search).
      - If it does, pull visitors to spill.chat daily.
      - If it does not, Harry exports the Website Visitors list from Apollo weekly, and the jobs import the CSV.
      - **[ASK HARRY]** if neither works.
  - **Per-account counts.** Apollo's domain aggregates for tracked domain spill.chat, for each queued or enrolled account that has an Apollo organisation id, counting only paths under /us.
  - **An unknown visiting company** is enriched in Apollo, then run through the hard filters and Clay with source = site_visit. It joins the queue if it passes.
  - **A visitor already in the queue** simply scores higher.
  - **A visitor already enrolled** is logged as a site_visit event and listed under "Warm accounts" in the daily post. It is not alerted.
  - **Customers, open deals and accounts owned by others** in HubSpot are ignored.
  - **The readout** separates visits before an account's first email from visits after it.
  - **Tracker fixes for Harry in phase 0:**
      - Change "us/pricing" to "/us/pricing".
      - Add "/us/book-demo" as high intent.
      - Confirm data is arriving.
      - Confirm the script is not on employee-facing pages.
## 8. Clay functions (built in the folder "US Outbound")
The jobs call these functions from Python through Clay's function API. In phase 0, confirm that the plan allows creating functions and calling them through the API. If it does not, use CSV import and export for the same two tables, and tell Harry what manual step that adds.
### "US Outbound – Accounts"
Input: domain, raw_name, apollo_hq_state, apollo_employees, apollo_industry.
Output: strict JSON. The raw output is stored in raw_clay_accounts.
```json
{
  "clean_name": "Acme Creative",
  "legal_name": "Acme Creative LLC",
  "domain_confirmed": "acmecreative.com",
  "hq_city": "Chicago", "hq_state": "IL",
  "employees": 64, "employees_source": "linkedin",
  "industry": "Advertising agencies",
  "founded_year": 2014,
  "pages": {"careers": "https://…", "benefits": "https://…", "values": "https://…"},
  "read_status": "read | no_pages_found | blocked | error",
  "benefits": [{"item": "mental health days", "quote": "…", "url": "…"}],
  "mental_health_provision": [{"type": "eap | carrier_eap | named_vendor | therapy_stipend | general_support | mental_health_days", "provider": "ComPsych", "quote": "…", "url": "…"}],
  "culture_statements": [{"quote": "…", "url": "…"}],
  "values_page": true,
  "funding": {"stage": "Series A", "amount_usd": 8000000, "date": "2026-07-14", "source": "…"},
  "credits_used": 4.5
}
```
Rules:
  - The page reader extracts what the pages say. It does not judge whether anything matches a signal; matching happens in Python against the sheet, so changing a signal never needs a Clay edit.
  - Use the cheapest Claygent model that passes the phase-1 hand-check.
  - A failed read (blocked or error) is scored as "not read", never as "nothing found": it adds no points and does not clear a Hold.
  - Name cleaning uses Clay AI, checked against the rules in section 13.
### "US Outbound – Contacts"
Input: full_name, domain, linkedin_url, title.
The function wraps the existing Work Email function and validates the result.
Output: {"email": "...", "status": "valid | catch_all_valid | invalid | not_found", "provider": "...", "credits_used": 1.0}.
### Budget
  - Before each batch, compute the month's remaining Clay budget: clay_monthly_credits minus the credits recorded this month.
  - Stop sending accounts to Clay once the budget is used.
  - Measure and report the real credits per account on the first 100 accounts.
  - Also set a workbook spend limit in Clay if the plan offers one.
## 9. Jobs, scoring and enrolment
All times are UK time. Every job writes a heartbeat row; a missed heartbeat alerts in Slack.
|  |  |  |  |
| :- | :- | :- | :- |
| **Job** | **Schedule** | **Does** | **Gate** |
| settings_sync | 02:00 daily, and on demand | Syncs the sheet into settings, validates, versions, then triggers score | An invalid tab keeps the previous version and alerts |
| source_universe | 1st of the month, 03:00, and on demand | Apollo organisation and people search across active industries × states × size bands; loads the IRS BMF; dedupes on root domain against accounts, the whole HubSpot portal and suppression | Every account has a domain, HQ state, employee estimate and industry |
| apollo_signals | Mon 03:30 | Free Apollo reads for queued, enrolled and top-scoring provisional accounts |  |
| site_visits | 06:00 daily | Discovery, per-account counts, sourcing of new visitors |  |
| public_signals | Mon 04:00 | Job-post feeds; layoffs and WARN notices |  |
| verify_in_clay | 04:30 weekdays | Picks accounts in order of provisional score to keep the queue 10 working days deep, within the Clay budget; calls the Accounts function; writes facts | Clay and Apollo agree on HQ state and size band, otherwise the account goes to the hand-check; read_status recorded |
| score | After settings_sync, verify_in_clay and site_visits | Sets score, tier and angle | Each tier is 5–40% of the month's queue; otherwise alert |
| pick_contacts | 05:30 weekdays | Applies the role rule via Apollo people search; Apollo bulk match (verified emails only); misses and catch-alls go to the Clay Contacts function | Verified email; not in CA or WA; not a personal domain; not suppressed; not a customer domain |
| enrol | 12:00 weekdays (07:00 ET) | Works out today's number (below), re-checks HubSpot, assigns the test version, renders copy, bulk-adds each lead to its sender's campaign | Within capacity and budgets; not a blackout date; live_sending = yes |
| poll_replies | Every 15 min | Reads the Instantly emails API, filtered to registry accounts; classifies; routes (section 11) | Idempotent on email id |
| poll_approvals | Every 5 min | Reads Slack thread replies on open alerts; sends approved replies through Instantly | Only approver ids count |
| hubspot_readback | Every 15 min | Reads meetings and deals on US Outbound companies; stops leads when a meeting is booked; creates the deal (section 11) |  |
| sync_outcomes | 01:00 daily | Instantly step analytics into events; deletes leads 31 days after their last step; applies Apollo deletion notices |  |
| mailbox_health | 07:00 daily | Instantly account and vitals status; seed-inbox placement; sends against cap |  |
| kill_rules | Hourly | Section 12 |  |
| daily_post | 09:00 daily | Section 11 |  |
| monday_readout | Mon 09:00 | Section 12 |  |

### Scoring
1.  **Score.** Sum the weight of every active Score signal that has a fact fresher than its counts_for_days. Cap each signal at its max_weight, and cap the total at score_cap.
2.  **Tier.** Take the first rule that applies:
    1.  Excluded: a hard exclusion (below) or an Exclude signal applies.
    2.  Held: a Hold signal applies.
    3.  Priority: score ≥ priority_threshold.
    4.  Standard: score ≥ standard_threshold.
    5.  Control: everything else.
3.  **Suppress.** A Suppress signal adds the domain to suppression for its counts_for_days.
4.  **Angle.** Use the first angle, in Angles-tab order, that any fresh signal suggests. Control accounts always get General.
5.  **Opener.** Use the opener template of the signal that set the angle, filled with its evidence. If that signal has no opener, use the angle's default_opener.
### Hard exclusions (fixed in code)
  - Brokers, insurers, HR-tech vendors, PEOs (NAICS 561330), HR consultancies (541612) and behavioural-health providers. These go on a partner list and are never prospected.
  - Anything in HubSpot matching any of these:
      - a customer
      - an open deal
      - an active sequence
      - an opted-out or bounced contact
      - an owner other than Harry
      - any activity by another user in the last 90 days
  - More than 20% of US-located staff in CA or WA (or in FL, until it is switched on).
  - Fewer than 5 US-located people found.
  - Founded less than 2 years ago.
### Daily enrolment number
It is the smallest of:
  - daily_enrol_cap;
  - the sum of the Active mailboxes' daily caps ÷ 4;
  - the remaining Clay budget ÷ working days left in the month ÷ measured credits per account;
  - the same calculation for Apollo;
  - the size of the verified queue.
Of that number, control_share comes from the Control tier. The rest comes from Priority, then Standard, ordered by score, then by size band (20 to 99 first), then by industry priority.
### Test assignment
  - The version is hash(account_id + test_id) % 2. This is deterministic, so each account's version never changes.
  - Both contacts at an account get the same version.
  - Only one test runs at a time.
### Instantly campaigns: one per sender
There is one campaign per mailbox owner: "US Outbound – Hannah Spalding", "US Outbound – Sam Jackson" and "US Outbound – Harry Dryden" (Harry's two addresses share his). Separate campaigns are what guarantee sender continuity, because the jobs choose the campaign and Instantly never rotates an account to a different person. All of them use the settings below. Create them through the API, then check every day that their settings have not drifted.
|  |  |
| :- | :- |
| **Setting** | **Value** |
| Schedule | Mon–Fri 09:00–16:00 America/New_York |
| Sending accounts | The owner's Active addresses in the registry |
| Daily limit | The sum of their caps |
| Steps | Four, at day 0, 3, 8 and 15, one variant each |
| Stop on reply | On |
| Stop for company | On |
| Stop on auto-reply | Off (the jobs handle out-of-office replies) |
| Text only | On |
| Open and link tracking | Off |
| Unsubscribe header | On |
| Risky contacts | Off |
| Evergreen | On |

The jobs render each lead's subjects and bodies into custom variables ({{s1_subject}}, {{s1_body}} and so on). Test the custom-variable length limit in phase 0. If it is too short, switch to a template with smaller variables ({{opener}}, {{proof}}, {{ask}}, {{price_line}}, {{signature}}).
### Sender continuity
  - **Assignment.** A sender is assigned to an account when it is first enrolled and stored on the account (accounts.sender). New accounts go to the sender with the most free capacity that day.
  - **Same sender throughout.** Every step, every approved reply, any second contact at the account (phase 3) and any re-contact after the 6-month rule go through that sender's campaign. In phase 0, verify that Instantly sends steps 2 to 4 from the same address as step 1 within a campaign, which matters for Harry's two addresses.
  - **Replies** are always sent from the mailbox the prospect wrote to, signed by its owner.
  - **Demos are always with Harry.** Emails from Hannah or Sam say so: "my colleague Harry Dryden runs our US demos; you can grab a time with him here", followed by booking_link. Emails from Harry say "grab a time with me". Harry is the HubSpot owner of every warm lead whoever the sender is.
  - **Pause and retire.** If a sender is paused, their accounts wait rather than move to someone else. A sender is only retired once their leads have finished (the 30-day retire wait covers this). An account whose sender has left gets a new sender only for a later sequence.
### Mailbox registry commands
|  |  |
| :- | :- |
| **Command** | **What it does** |
| mailbox add | Adds the address to the sheet, turns warmup on, puts it on its owner's campaign's sending list through the accounts API (creating the owner's campaign if it is a new person), and sets it to Warming. It is promoted to Active after 21 days, or at once if it is already warm. |
| mailbox pause | Takes it off the sending list. Warmup and reply reading continue. |
| mailbox retire | Pauses it, then removes it from its campaign and the registry after 30 days, once its leads have finished. |

## 10. Copy
### Sequence
|  |  |  |
| :- | :- | :- |
| **Step** | **Day** | **Content** |
| 1 | 0 | Plain text. One untracked link, to the privacy and opt-out page, and nothing else. |
| 2 | 3 | Adds one proof point and Harry's demo link. |
| 3 | 8 | Short, one question. |
| 4 | 15 | Break-up, with the one-pager. |

Every step ends with:
  - the sender's name and role, from the mailbox registry
  - the postal address
  - one line saying the email is marketing from Spill
  - "Reply STOP or use this link to opt out"
  - the privacy link
Step 1 also says where the contact data came from, as UK GDPR Article 14 requires.
### Variables
|  |  |
| :- | :- |
| **Variable** | **Content** |
| opener | From the evidence behind the angle |
| proof | By industry group. Use a named US customer once one exists; until then, a UK analogue labelled as UK |
| place | HQ city or state |
| ask | By role. People leader: "a 20-minute walkthrough". Founder or executive: "worth a look for the team?". Operations: "happy to send the one-pager". When the sender is not Harry, the demo ask names Harry as the host |
| price line | By size band, from the prices in section 4 |
| signature | From the mailbox registry |
| demo line | From the sender: "grab a time with me" (Harry) or "my colleague Harry Dryden runs our US demos; you can grab a time with him here" (Hannah, Sam), then booking_link |
| legal overlay | Legal Teams only |

### Rules
A render-time check blocks the send if any of these fail:
  - "Counselor" and "counseling", never "therapy" or "therapist".
  - "Registered", never "licensed".
  - "Same day".
  - No "unlimited".
  - Never disparage the prospect's existing EAP or benefits.
  - "EAP" is fine when naming theirs, but never in Spill's product name.
  - American spelling.
  - The 30% utilisation figure is the only statistic allowed.
  - No empty or unrendered {{variable}}.
  - No line over 300 characters.
  - Every copy row sent has status approved.
Tests also render every template against empty and maximum-length values.
## 11. Replies and the human in the loop
### Classification
Replies are classified by the Claude API with a dedicated key, capped by claude_monthly_cap_usd. The output is JSON:
|  |  |
| :- | :- |
| **Field** | **Values or content** |
| class | positive, referral, objection, not_now, negative, out_of_office, wrong_person, unsubscribe, other |
| demo_requested | Boolean |
| objection | price, have_eap, have_vendor, timing, not_decision_maker, other |
| not_now_date | Date |
| ooo_return_date | Date |
| referral_name, referral_email |  |
| competitor_named |  |
| language_terms |  |
| summary | One line |
| confidence | 0 to 1 |
| draft_reply | Positive and referral replies only. Written in the mailbox owner's voice and signed as them, obeying the copy rules. It uses the demo line for that sender, so replies from Hannah or Sam hand the demo to Harry with booking_link |

A reply with confidence below 0.7 is treated as "other".
### Routing
|  |  |
| :- | :- |
| **Class** | **What happens** |
| positive, referral | The HubSpot writes below, then an instant Slack alert in #us-outbound mentioning Harry and the mailbox owner |
| not_now | The date is stored; a reminder goes in the daily post a week before it |
| objection, negative, other | Daily post only |
| unsubscribe, or a negative reply asking to stop or be removed | Added to suppression and the Instantly blocklist, and set to opted out in HubSpot if the contact exists there |
| out_of_office | The return date is stored; daily post |
| wrong_person | pick_contacts re-runs for the account; the new contact appears in the weekly hand-check |

### Slack alert
Sent with Block Kit through the Web API, using a bot token.
```
Positive reply · Acme Creative (Chicago, IL) · Marketing agencies · Priority
From: Jane Doe, Head of People → hannah@meetspill.org
"Sounds interesting, could you send over some times next week?"
Why this account: Benefits page lists an EAP and mental health days; new Head of People (Aug)
Draft reply (signed Hannah Spalding; hands the demo to Harry):
> Hi Jane, great to hear. My colleague Harry Dryden runs our US demos; you can grab a time with him here: https://meetings.hubspot.com/harry336/us-demo-link …
HubSpot: <link>
Reply in this thread: "send" to send the draft · "send: <your text>" to send your version · "skip" to handle it yourself
```
### Approval
poll_approvals reads thread replies from Harry only:
  - "send" sends the draft.
  - "send:" followed by text sends that text.
  - "skip" marks the alert handled.
The reply goes out through Instantly's reply endpoint from the mailbox the prospect wrote to. The job then confirms in the thread ("Sent from <hannah@meetspill.org> at 14:32 UK") and logs an events row with the approval.
### Escalation to <harry@spill.chat>
  - **2 hours.** An alert unanswered for 2 hours during 13:00–21:00 UK is re-posted, mentioning Harry.
  - **24 hours.** Any human-in-the-loop item not actioned within escalation_hours is emailed to escalation_email (<harry@spill.chat>). This covers reply approvals, the weekly hand-check, manual merges and kill-rule alerts.
      - Reply items are forwarded through Instantly's forward endpoint from the mailbox that received them, so Harry sees the original thread, with the draft, the Slack link and the HubSpot link added. Confirm that endpoint exists in phase 0.
      - Everything else, and replies if the forward endpoint is missing, becomes a HubSpot task for Harry due now. HubSpot emails its task notification to him.
      - The email says how to act: reply "send" in the Slack thread, or reply from the sender's mailbox. Never reply to the prospect from <harry@spill.chat>.
  - **Enrolment pause.** While any positive reply has waited more than 24 hours, new enrolment pauses. Sequences already running continue.
### HubSpot writes (positive or referral replies only)
1.  Find the company by root domain.
      - If more than one company matches, hold the account and flag it for a manual merge.
      - If none matches, create the company with the clean name, domain and the four company properties.
2.  Find the contact by email, or create it. Associate it with the company and set the two contact properties.
3.  Add the reply as a note, and a task for Harry due today.
4.  Set lifecyclestage = lead, hs_lead_status = CONNECTED and hubspot_owner_id = Harry, each only if it is empty.
5.  Deal. If demo_requested is true, or hubspot_readback sees a meeting booked on Harry's demo link or the website booking page, create one deal named "US Outbound – {clean_name}" in pipeline "Spill 3.0" at its first stage. Only do this if the company has no open deal.
The six properties, in the group "US Outbound":
|  |  |
| :- | :- |
| **Object** | **Property** |
| Company | us_outbound_account_id (unique) |
| Company | us_outbound_tier |
| Company | us_outbound_industry_group |
| Company | us_outbound_top_signals (text with evidence) |
| Contact | us_outbound_angle (angle and copy version) |
| Contact | us_outbound_reply_class |

### Daily post (09:00 UK)
One message covering:
  - objections, negatives and "other" replies
  - out-of-office replies
  - warm accounts: enrolled accounts that visited the US site, pricing and demo pages first
  - not-now dates coming due
  - mailbox health
  - credits used against budget
  - any kill rule that fired
### Weekly hand-check (Monday, before that week's enrolment)
Ten random queued accounts per active industry group are posted, each with its rendered step-1 email and its evidence. Harry approves them or pulls individual accounts. Enrolment waits until he has.
## 12. Learning, kill rules, readout
### Copy tests
  - Two versions, split 50/50 by account, running to 400 accounts per version (about six to seven weeks at starting volume).
  - Each test is read once, on its pre-registered date, on reply rate: human replies within 21 days of step 1 ÷ accounts with step 1 delivered.
  - A test detects a 2× difference. Positive and meeting rates are reported, but do not pick the winner in v1.
  - The first test: an "Upgrade the EAP" opener against a "General" opener, on accounts where an EAP is named.
### Signal value
v_signal_value, shown in the Monday readout, reports for each active signal:
  - how many accounts showed it
  - their reply rate and positive-reply rate
  - the Control tier's rates, for comparison
A signal is flagged if its accounts reply below the Control rate after 200 accounts. Harry changes weights in the sheet; nothing re-weights itself.
### Quarterly source review
For each source: coverage, accuracy from the hand-checks, and credits per account.
### Kill rules
These run hourly, and every one alerts in Slack.
|  |  |
| :- | :- |
| **Trigger** | **Action** |
| A domain's bounce rate is over 3% on 100+ sends | Pause that domain's mailboxes in the registry |
| A mailbox gets a 5.7.x block bounce, fails its vitals check, or lands in spam in a seed inbox | Pause it for 14 days |
| An email source (Apollo verified or Clay waterfall) has bounces over 3% | Pause the source until checked |
| An industry group's reply rate is under 0.5% after 400 delivered | Stop enrolling it |
| Any human-in-the-loop item left for 24 hours | Email it to <harry@spill.chat>; while a positive reply is waiting, pause new enrolment |
| Apollo credits fall below apollo_floor, or the Clay budget is used | Stop new verification; enrolment continues from the verified queue |
| A settings sync fails | Keep the previous version |
| Fewer than stop_rule_meetings meetings from the first stop_rule_accounts accounts | Pause enrolment for a profile and copy review |

### Monday readout (09:00 UK)
  - accounts enrolled
  - reply rate by industry group and by copy version
  - meetings booked, and time per meeting
  - the signal table
  - the running test against its read date
  - site visits before and after the first email
State and role cuts are added monthly. Below the fold, Claude adds three observations drawn from the reply text: objections, language used, and competitors named.
Working assumption for the funnel: a 3 to 5% reply rate, about 1% positive replies, and 0.5 to 1% of accounts reaching a meeting. At starting volume that is 3 to 6 meetings a month.
## 13. Deliverability, compliance and operations
### Deliverability
**Mailboxes:** the four in the registry, on two domains (meetspill.org with three, tryspill.org with one). They are dedicated to US Outbound and used by no other campaign.
**Domains.** For each domain:
  - Check blacklists and prior use.
  - Confirm it is registered in Spill's name.
  - Confirm it redirects to spill.chat/us.
  - Set up SPF, DKIM and DMARC at p=none with reporting; move to p=quarantine after 30 clean days.
**Sending:**
  - 30 sends per mailbox per day, with warmup always on.
  - Four seed inboxes (2 Google, 2 Microsoft 365) receive every step and are checked weekly.
### CAN-SPAM
CAN-SPAM applies to B2B email. Every email needs:
  - accurate headers, and a From line naming a real Spill person;
  - no misleading subject line;
  - the postal address, the advertisement line and a working opt-out.
Opt-outs are honoured the same day. Leads stay in Instantly for 31 days after their last step, and no mailbox is retired within 30 days of its last use.
### Privacy
  - **Not in scope**
### Data cleaning
Applied in clean/ before any row is stored:
  - **Company name:**
      - Start from Clay's cleaned name, checked against our rules.
      - Strip Inc, Inc., LLC, L.L.C., Ltd, Corp, Co., PLLC, LLP, PC and P.A.
      - Strip taglines after " | ", " – " or ":", and bracketed qualifiers such as "(formerly …)".
      - Strip phrases such as "Official Site".
      - Keep the legal name separately.
  - **Domain:** the root domain, lower case, without www. Follow one redirect, and keep an alias list.
  - **Person:**
      - Title-case names, with credentials and pronouns stripped.
      - Map titles to roles.
      - Record states as USPS codes.
  - **Dedupe:** one account per root domain; one contact per email hash.
  - **Overrides** win over everything.
### Operations
**CLI.** These are the same entrypoints as the jobs. Run them locally or through gcloud run jobs execute:
  - status
  - stop / start: pause or resume all US Outbound campaigns and enrolment
  - `mailbox add|pause|retire <address>`
  - unenrol --month YYYY-MM
  - rescore
  - `dry-run <job>`
  - `erase --email <address>`
  - `test start|read <test_id>`
**Health.** Every job writes a heartbeat. A daily check confirms that each US Outbound campaign's settings and sending list match the sheet and the registry.
**Secrets:**
  - Apollo master API key
  - Clay API key
  - Instantly API key
  - HubSpot private-app token (create it before 26 Oct 2026)
  - Slack bot token
  - Google service account
  - Claude API key
**HubSpot token scopes:**
  - read and write: contacts, companies, deals, lists
  - read: owners, meetings, pipelines
  - write: notes, tasks and communication preferences
  - schema write for the property group
**Repository.** One repository, us-outbound, with this file as SPEC.md and a short README.md. Tests use pytest. Harry reviews before each deploy.
```
us-outbound/
  SPEC.md  README.md  pyproject.toml
  us_outbound/
    clients/     apollo.py clay.py instantly.py hubspot.py slack.py sheets.py bq.py claude.py
    settings/    sync.py validate.py conditions.py
    sources/     apollo_org.py apollo_people.py apollo_jobs.py site_visits.py clay_careers.py job_posts.py irs_bmf.py layoffs.py calendar.py
    clean/       names.py domains.py people.py
    scoring/     score.py tiers.py angle.py
    contacts/    pick.py verify.py
    enrol/       queue.py render.py copy_rules.py enrol.py
    replies/     poll.py classify.py approvals.py send.py
    crm/         hubspot_writes.py readback.py
    registry/    mailboxes.py
    learn/       tests.py readout.py daily_post.py kill_rules.py
    ops/         cli.py heartbeat.py erase.py
  sql/           ddl/ views/
  templates/     slack/ prompts/
  tests/
```
## 14. Build plan
### Phase 0: Foundations (29 Sep – 9 Oct)
Deliverables:
  - **Confirm plan facts:**
      - Clay: tier; monthly credits and actions; whether functions can be created and called through the API; whether spend limits exist.
      - Instantly: plan; email and uploaded-contact caps, and how much other campaigns already use; whether the emails, reply and accounts endpoints are included.
      - HubSpot: tier and remaining custom-property allowance; ids for the "Spill 3.0" pipeline and its stages.
      - Apollo: when credits lapse; whether visitor discovery is available through the API.
  - **Repository and infrastructure:** the repository, clients and Secret Manager; the BigQuery DDL.
  - **Settings sheet:** created and loaded with the section 5 defaults, plus settings_sync with validation.
  - **Mailboxes:** the four registry mailboxes checked for warmup status and domain authentication.
  - **HubSpot:** the token and the six properties.
  - **Clay:** the folder and the two functions, tried on 20 hand-picked accounts.
  - **Instantly:** the three sender campaigns created and left paused. Test:
      - the custom-variable length limit
      - that follow-up steps stay on the step-1 address
      - whether a forward endpoint exists
  - **Slack:** the app, #us-outbound and #us-outbound-dev.
  - **Dry-run** everywhere.
  - **Suppression** loaded from HubSpot opt-outs and bounces.
  - **For Harry to approve:** the footer text, the privacy page link and the legitimate-interests text.
Acceptance:
  - One test record flows end to end in dry-run.
  - Changing a weight in the sheet changes a test account's score after the next sync.
  - A bad row in the sheet is rejected, with a Slack message.
  - A test that inspects every client call finds no write outside the allowed containers.
### Phase 1: Universe 
Deliverables:
  - Apollo sourcing for Technology & Startups and Marketing & Creative Agencies across the seven active states.
  - IRS BMF loaded.
  - Job-post feeds.
  - The site-visits job.
  - Clay verification of the first queue.
  - Scoring and tiering.
Acceptance:
  - Harry hand-checks 10 random queued accounts per industry group. Clean name, HQ state, size band, role title and opener evidence are right in 90% or more.
  - Clay credits per account are measured and reported.
  - Mailboxes are warm at 30 sends a day.
### Phase 2: First sends 
Deliverables:
  - pick_contacts.
  - enrol, going live only after **[ASK HARRY]** sign-off.
  - poll_replies, classification, the HubSpot writes, Slack alerts and approvals, hubspot_readback and sync_outcomes.
  - The first copy test registered, with its copy approved by Harry in the sheet.
Acceptance:
  - Bounce rate under 3%.
  - A positive reply reaches HubSpot and Slack within 20 minutes.
  - An approved reply is sent from the correct mailbox, signed by its owner.
  - The 50/50 split is verified.
### Phase 3: Learning loop 
Deliverables:
  - The daily post, and the Monday readout with its signal table.
  - Kill rules live.
  - A second contact for Priority accounts that finished the sequence without a reply.
  - Great Place To Work and B Corp lists added as sources.
  - Nonprofits and Legal Teams queued for January.
  - Wave-2 states, once Harry approves them.
Acceptance:
  - The readout runs unattended for two weeks.
  - The first test is on track for its read date.
### Phase 4: Scale 
  - More mailboxes.
  - A larger Clay budget.
  - Professional Services.
  - The "Switch from a competitor" angle.
  - LinkedIn company-list audiences.
  - Clay Signals.
  - IRS 990 employee counts and Form 5500 as sources.
## 15. Open items
|  |  |
| :- | :- |
| **Item** | **Default until Harry answers** |
| Roles in signatures (optional) | Full name, "Spill" and spill.chat/us |
| A Clay workflow Harry may want copied into the folder | None, unless Harry shares one |
| The footer's advertisement line | Include one short line |
| Florida | Off |
