# US Outbound: lead-quality audit

Scope: how target accounts are found, verified, scored and tiered, and how the one contact per account is chosen. Everything below was checked against the code in `/home/user/columbus` (commit `cb0bf8f`, 1 Oct 2026). The test suite passes (1,273 passed, 34 skipped). Scoring and title mapping were also probed locally against the real sheet defaults, without calling any external service.

## Bottom line

- **Most of the lead-quality layer is not built yet.** The scorer, the tiers, the queue and the send gate are built and tested. Building the universe, enrichment, Clay verification, the signal sources and contact choice is all still to do. Only one source module exists (`sources/named.py`), and `contacts/` holds an empty `__init__.py`. So the quality of the leads depends on decisions that are still open, which makes now the cheapest time to change them.
- **Contact choice is the biggest risk.** No code applies the role rule. The default title lists leave out most real HR and owner titles. There is one contact per account, and that contact is not tied to the signal that made the account interesting.
- **The score is mostly static fit, and it has no decay.** The strongest timing signals are either unbuilt (site visits, new People leader), mis-wired ("Hiring and growth"), or outweighed by common careers-page features. A carrier-bundled EAP alone scores +35.
- **The ICP rests on assumptions that haven't been tested**, with no lookalike step and no HubSpot history behind it. The two active verticals (Tech, and Marketing & Creative) are defined loosely. The tech labels share one set of NAICS codes, including IT services.
- **The learning loop cannot correct this quickly.** At about 1% positive replies and about 650 accounts a month, `v_signal_value` will not separate good signals from bad ones for many months. Its attribution also changes on every rescore.

---

## 1. The real data flow

| Step | Built (file:line) | Only in the spec or docs | Missing or risky |
| :- | :- | :- | :- |
| **Universe** | The front door is `accounts.admit` (`accounts.py:37-62`): root domain, alias lookup, personal-domain, partner and suppression checks. The Named accounts source is built (`sources/named.py:56-80`). The Apollo client has `search_organizations` (`clients/apollo.py:149`) | `source_universe` slicing by state × industry × size band, the Python NAICS/keyword re-check, the IRS BMF and site visitors (SPEC 7, 9; pipeline.md "The funnel") | The job is a stub: `ops/cli.py:73` says "not built yet (phase 1)", and `ops/schedule.py:36` has `enabled=False`. `admit` does **not** dedupe against HubSpot, though pipeline.md:76 says it does. `follow_redirect` (`clean/domains.py:184`) is never called, so redirects aren't followed and no aliases are recorded |
| **Enrichment** | Clients only: `Apollo.enrich_organization`, `Clay.run_function_batch` and `parse_accounts_output` (`clients/clay.py:122, 295`) | `verify_in_clay`; the "Which value wins" resolver (pipeline.md:354-372) | No resolver writes `hq_state`, `employees`, `size_band` or `industry` to `accounts`. A real account today would be Excluded as "HQ state unknown" (`scoring/tiers.py:201-203`), and its size band would rank 4, after all real bands, in `v_queue` |
| **Verification** (company) | Only the Clay output validator | The Clay vs Apollo HQ and size-band check sends disagreements to the hand-check (SPEC 9) | Not built. The Clay Accounts and Contacts functions don't exist in Clay yet (phase0-facts.md:289) |
| **Signals** | The matcher and freshness logic (`scoring/score.py:119-345`). A safe condition parser (`settings/conditions.py`) | apollo_people, apollo_jobs, site_visits, job_posts, irs_bmf, layoffs (SPEC 7) | Only `named` writes facts. The site-visit tracker has received no data (`data_received: false`, phase0-facts.md:301) |
| **Score / tier / angle** | `score_account` (`score.py:355-383`), `tiers.tier` (`tiers.py:257-279`) and `choose_angle` (`angle.py:97-115`). `rescore` runs inside settings_sync | | No decay. The tier-mix bounds are hard-coded (`score.py:55`) |
| **Contact** | Only the send gate: `contact_block` (`enrol/enrol.py:214-238`) and `pick_contact` (`enrol.py:241-250`). Title→role mapping exists (`clean/people.py:258-277`). Apollo `search_people` and `bulk_match` have the waterfall off (`apollo.py:47, 159, 164`) | `pick_contacts`: role rule, Apollo bulk match, Clay waterfall on misses and catch-alls, fallback order (SPEC 9; pipeline.md:108-112) | **`pick_contacts` does not exist** (`cli.py:79`). `map_title_to_role` has no caller. `first_choice_for_size` and `fallback_order` are validated (`settings/validate.py:694-732`) but never read. `pick_contact` takes "the first sendable one by created_at", not the best role |
| **Queue** | `order_key` (`enrol/queue.py:72-81`), the control share (`queue.py:84-91`), focus quotas (`enrol/focus.py:222-231`) and the `v_queue` view | | It orders by tier, then score, then size band. There's no recency. On ties, the oldest `first_seen` goes first |

## 2. Every signal, as defaulted

Sources: `settings/defaults.py:154-240`. Weights, windows and actions were confirmed by loading the defaults through `validate_all`.

| Signal | Comes from | Weight | Window | Kind | Likely predictive strength | Notes |
| :- | :- | :- | :- | :- | :- | :- |
| Mental health support listed | Clay page read (`clay_careers`): benefits and provision text | +25 | 540 d | Fit | Medium. Shows budget and that they care, but often means "already served" | Its terms include "EAP" and "employee assistance", so it fires together with "EAP named". `fact_text` also appends Clay's provision *type* (`score.py:192-193`), so any `therapy_stipend`, `carrier_eap` or `mental_health_days` row matches |
| EAP named | `clay_careers` | +10 | 540 d | Fit | Low to medium. Carrier-bundled EAPs come with most group insurance | Probe: a carrier EAP alone scores 25+10+10 (Q4) = **45, Standard**. Adding a values page makes it **Priority**. The vendor list (ComPsych, GuidanceResources, Magellan) leaves out Optum, Carelon, Cigna, Aetna Resources For Living, TELUS Health and Health Advocate |
| Modern mental-health vendor named | `clay_careers`, `job_posts` | 0, **Hold** | 540 d | Fit (negative) | Right for Lyra, Spring, Modern Health, Talkspace, BetterUp, Nivati and Tava. Wrong for Calm, Headspace, Wellhub and Gympass, which are apps or fitness and complement counseling | Held accounts have no exit, review list or alert. Only a count shows (`score.py:549`). The angle is inactive |
| Progressive benefits | `clay_careers`, `job_posts` | +10 each, max +30 | 540 d | Fit | Low. Unlimited PTO and parental leave are standard in tech | Matches singular whole words only (open-questions #14) |
| Culture or values page | `clay_careers` `values_page` | +10 | 540 d | Fit | Very low; almost every company has one | |
| People leader in place | `apollo_people` `people_leader_count` | +10 | 365 d | Fit | Medium: someone owns benefits | Which titles count isn't defined anywhere, because the source is unbuilt |
| New People leader | `apollo_people` `people_leader_days_in_title ≤ 90` | +30 | 90 d | **Timing** | **High** | Depends on Apollo's free people search returning title start dates, which is unconfirmed. At 10–49 staff the role rule would email the founder, not the new leader |
| First People hire | `apollo_jobs` + `apollo_people` | +25 | 90 d | Timing | Medium to high | Needs an explicit `people_leader_count = 0`. A missing fact never matches (`conditions.py:80-81`). Probe: open People roles alone score 0 |
| Recent funding | `apollo_org`, `clay_funding` | +20 | 540 d | Timing, but weak at 540 d | High within 6 months, low at 12–18 months | Same weight on day 1 and day 539 |
| Hiring and growth | `apollo_org` only | +15 | 90 d | Timing-ish | Low to medium; ≥3 open roles is a low bar | **Wiring bug.** pipeline.md:336 and 372 make `apollo_jobs` the only owner of `open_roles`, but the row reads `apollo_org` (`defaults.py:206`). Probe: 5 open roles from `apollo_jobs` score **0** |
| Visited the US site | `site_visits` | +20 | 30 d | **Intent** | Very high, but tiny volume | No `suggests_angle`, so the angle is General. General gets **no opener** (`enrol.py:365`). Even with the pricing view it reaches only 45, which is Standard |
| Viewed US pricing or demo page | `site_visits` | +15 | 30 d | Intent | Very high | Same problem |
| Nonprofit budget / fiscal year ahead | `irs_bmf` | +15 / +20 | 400 d / 1 d | Fit / Timing | Fiscal year ahead is strong | Nonprofits are off in v1 |
| Q4 plan-year window | `calendar` | +10 | daily | "Timing" | **No power to tell accounts apart** | Adds 10 to every account in Oct–Dec. Any single signal then reaches Standard, and the tiers shift back on 1 January |
| Layoffs | `layoffs` | Suppress 90 d | 90 d | Timing (negative) | Sensible for tone | Unbuilt |
| Named by Harry | Named accounts tab | +30 | 365 d | Manual | Unknown | The only source that writes facts today |

**Freshness.** Clay facts count for 540 days, but SPEC 7 re-reads pages only after 180 days, and that job is unbuilt. Scoring is binary: a fact counts in full until its window ends, then counts nothing (`score.py:119-128, 229-237`). Facts that count days, such as days since funding, are aged correctly (`score.py:240-249`).

**Inputs collected but never scored.** `states_with_staff` (`settings/model.py:48`) has no signal, although SPEC 5 defines "Remote & hybrid teams" as a flag for staff in 3 or more states. Clay's "Website Technology Stack" and "Website Traffic" functions exist (phase0-facts.md:27) but are unused. They could detect Slack or Teams, which is where Spill is delivered, and HRIS or PEO tools.

## 3. The ICP definition

**Size.** Four bands, 10-19, 20-49, 50-99 and 100-249 (`model.py:78`). Size is enforced only by the Apollo search filter and `size_band()`, which returns None outside 10–249 (`people.py:287-299`). `hard_exclusion` never checks size (`tiers.py:185-230`). The queue puts 20–99 first and 10–19 last (`queue.py:40`). The copy quotes one price, "from $195 a month for the whole team", to every size. At 10 staff that works out to about $19.50 an employee a month, against $1–3 for a typical EAP.

**Industries.** 27 active labels: 16 in Technology & Startups, 10 in Marketing & Creative Agencies, and Digital health, which the site files under Healthcare. Fifteen of the 16 tech labels share the same NAICS prefixes, `5112; 513210; 5415; 518210`, and differ only by Apollo keyword (`settings/data/industries.csv`). NAICS 5415 covers IT services, managed service providers and IT staffing, which are low fit and heavily prospected. The "Startups" keywords ("startup; venture backed; seed stage") overlap every other tech label, and the code that assigns a label is unbuilt. `proof_point` is blank on all 108 rows.

Digital health and Healthtech are active, but partner detection relies on NAICS codes 621330 and 621420 (`tiers.py:56-66`). Apollo rarely gives those codes to software telehealth companies, and "telehealth", "teletherapy" and "virtual care" are not partner keywords (`tiers.py:80-84`). So teletherapy startups, which are competitors, can enter. Partner keyword matching also reads `apollo_keywords` and `apollo_industry` (`tiers.py:87`), and no source writes those yet.

Nonprofits and Legal Teams wait until January, although their pages and copy are ready. The repo holds no evidence that Tech and Agencies convert best in the US; the choice looks like a carry-over from the UK.

**States.** Seven HQ states are active: NY, MA, NJ, PA, IL, GA and TX (`defaults.py:305`). The HQ must be active (`tiers.py:200-207`). The **contact** only has to be in a known US state other than CA or WA (`enrol.py:226-230`), so a contact in Florida at a New York company passes even while FL is "off". A company with more than 20% of US staff in CA or WA is excluded, and while FL is off its FL share is added in (`tiers.py:209-218`). That rule is right on compliance, but it removes much of the tech universe.

**Hard exclusions that are mis-sized.**
- "Fewer than 5 US-located people found" (`tiers.py:220-224`). Apollo's coverage of 10–19-person companies is thin, so this will wrongly drop many of them.
- "Founded less than 2 years ago" (`tiers.py:226-229`). This removes recently funded seed and Series A teams, which conflicts with the Recent funding signal.
- Every exclusion **fails open** when its fact is missing.

**Roles** (`defaults.py:315-333`). A probe of `map_title_to_role` with the defaults:

| Maps to a role | Maps to **nothing** (never contacted) |
| :- | :- |
| Founder & CEO, Managing Partner, Executive Director, President & COO, Co-Founder and CTO, Senior HR Manager, VP of People, Chief People Officer, Head of People & Culture, Office Manager, COO, Director of Operations, Chief of Staff | **Owner, Principal, Partner, Founding Partner, General Manager, Head of HR, VP Human Resources, Chief Human Resources Officer, Director of People, Director of People Operations, People Operations Manager, HR Generalist, HR Business Partner, Benefits Manager, Total Rewards Manager, Operations Manager, Director of Finance and Operations** |

Open-questions #36 already flags part of this. At 50–99 staff the HR person is most often an "HR Generalist", "People Operations Manager" or "Director of People", and none of those match. At 100–249 it is usually "VP HR", "Head of HR" or a CHRO, and none of those match either. At agencies and professional firms with 10–49 staff the decision-maker is often "Owner", "Principal" or "Partner". The likely result is that larger accounts fall back to the Office Manager or COO, and smaller ones lose their real buyer. "Co-Founder and CTO" counts as "Founder or executive", so a technical co-founder could be chosen ahead of the CEO, since no seniority ranking exists.

**10 versus 200 staff.** At 10–49 the rule is Founder or executive, then Operations. At 50–249 it is People leader, then Operations (rank 2), then Founder (rank 3) (`defaults.py:315-330`). None of this runs yet. Today `pick_contact` simply takes the earliest-created sendable contact (`enrol.py:241-250`). Finance is never contacted, even though at 100–249 staff the CFO often signs off on benefits spend.

## 4. Data quality controls

| Control | State | Evidence |
| :- | :- | :- |
| Email status gate | Built: `verified`, `valid` and **`catch_all_valid`** are sendable | `enrol.py:57`. pipeline.md change 8 says the catch-all decision is still open, and the campaigns run with "Risky contacts: Off" (SPEC 9), so a catch-all lead may be enrolled and never sent. Apollo's own catch-all flag is not read anywhere |
| Apollo reveal | Waterfall off; personal emails refused | `apollo.py:44-49, 164-167` |
| Personal domains, shared inboxes | Built, with good lists | `domains.py:32, 163`; `enrol.py:232-235` |
| Contact location | Must be a known US state, not CA or WA | `enrol.py:226-230` |
| Suppression | Hashed emails, domains and aliases. HubSpot opt-outs and bounces loaded daily | `suppression.py`; `enrol.py:175-193` |
| HubSpot conflicts | Checked **only at enrol**, after Clay credits are spent: customer, other owner, open deal, opted out | `enrol.py:376-399`. `hubspot_active_sequence` and `hubspot_other_activity_90d` are listed (`tiers.py:93, 96`) but **no code writes them** |
| Domain cleaning and dedupe | Root domain via the public suffix list, plus aliases | `domains.py:208`; `accounts.py:39-46`. No redirect is followed at the door, and parent or sister brands are not linked |
| Contact dedupe | Index only, no unique constraint on `email_sha256` | `sql/ddl/02_contacts.sql:34` |
| Name cleaning | Strong rules. Casing is kept, so "acme creative" stays lower case | `clean/names.py:11, 102`; open-questions #37 |
| Contact freshness | **None**: no `verified_at`, Apollo person id, LinkedIn URL or seniority on `contacts` | `02_contacts.sql`. Reveals are just in time (two days ahead), which helps |
| Re-contact rules | `recontact_*_months` are settings that **nothing reads** | grep finds no use outside `settings/` |
| Bounce kill rules | Spec only (phase 3) | `schedule.py:50` |

## 5. Use of credits

**Apollo** (2,000 a month):
- Organization search costs 1 credit per page of 100, which is about 0.01 a found account (`apollo.py:33`).
- Organization enrich costs 1 credit, and only when a field is missing.
- `bulk_match` costs about 1 credit per revealed email.
- People search is free.
- That comes to roughly 1–2.5 credits per enrolled account.

The sweep is the unknown. Seven states × 27 labels × 4 bands is 756 slices if the search runs per label, which could take a third or more of the month on its own. Slicing by industry group, with the union of its NAICS codes and keywords, would cut that sharply.

**Clay** (2,000 a month): per-account cost is **unmeasured**, and `clay_credits_per_account` defaults to 0 (`defaults.py:138`). SPEC 8's example shows 4.5 credits for the Accounts function, plus about 1 for the Contacts function on misses and catch-alls. To reach about 650 enrolled accounts a month after drop-off (Held, Excluded, no email, hand-check pulls), Clay must cost **2.5 credits or less per account**. At 4.5 credits it supports about 440 runs a month, roughly 100 enrolments a week, so Clay will probably cap volume below 150 a week.

**Is the spend going where it matters?** Mostly not.
- Clay is spent before the HubSpot check, and on the 15% Control share.
- It pays for name cleaning and HQ or size confirmation, which rules and Apollo already cover, alongside page reading, which is its real value.
- The free job-post feeds (Greenhouse, Lever, Ashby, Workable) carry benefits text for many tech companies. Yet they aren't a source for "Mental health support listed" or "EAP named" (`defaults.py:156, 164`).
- A second contact reveal costs about 1 Apollo credit and is very likely worth more than Clay name cleaning.

## 6. Weaknesses that would lower the share of interested leads, ranked by likely impact

1. **No real contact selection, and title lists that miss most buyers.** High impact.
   - `pick_contacts` is absent (`cli.py:79`), and `pick_contact` takes the earliest-created sendable contact (`enrol.py:241-250`).
   - Role ranks are parsed but unused (`validate.py:694-732`).
   - The default titles miss Head of HR, VP HR, CHRO, Director of People, People Ops Manager, HR Generalist, Owner, Principal and Partner (see the table in section 3).
   - There is no seniority ranking.
   - The contact isn't linked to the signal: a "New People leader" account at 30 staff would email the founder.
   - *Fix:* build pick_contacts with wider title lists and a seniority score. Prefer the person named in the signal, and prefer whoever owns benefits at each size: founder at 10–19, founder or ops/people at 20–49, HR at 50 and above.

2. **One contact per account, and no buying committee.** High impact. SPEC 2 fixes "one contact in v1". The second contact comes only in phase 3, only for Priority accounts that finished the sequence without a reply (SPEC 14). A bounce, a wrong person or a filtered inbox wastes the whole account, after the Clay spend. *Fix:* at 50–249, enrol two contacts (HR and founder/CEO) from the same sender. "Stop for company" already prevents collisions (SPEC 9).

3. **Timing is underweighted, has no decay, and the hottest signals are mishandled.** High impact.
   - Funding counts in full for 540 days.
   - Site visitors land in Standard with the General angle and **no opener** (`enrol.py:365`).
   - "New People leader" depends on an unconfirmed Apollo field.
   - Benefits renewal timing is missing: Form 5500 plan-year dates and broker or carrier names are free from the DOL, but deferred to phase 4.
   - Q4 is a flat +10 for everyone.
   - *Fix:* use decaying weights, for example funding at full weight up to 180 days and half at 365. Make site visits Priority with their own angle. Add plan-year and renewal signals.

4. **Weak exclusion of accounts already served.** Medium to high impact.
   - PEO clients (Justworks, TriNet, Insperity, ADP TotalSource), which usually come with a bundled EAP, are not detected. The Clay tech-stack function could find many of them.
   - Calm, Headspace, Wellhub and Gympass are put on Hold alongside direct competitors (`defaults.py:170`), so complementary buyers are lost. Held accounts have no review path.
   - A carrier EAP scores +35 through the term overlap between the two signals (`defaults.py:156-168`).

5. **Signal-wiring errors in the defaults.** Medium impact; quick to fix.
   - "Hiring and growth" never fires on `apollo_jobs` (`defaults.py:206`, probe confirmed).
   - EAP is counted twice.
   - A values page is worth +10 though nearly every company has one.
   - "Progressive benefits" rewards standard tech perks up to +30, so Priority will skew towards typical VC-backed startups. Those are also the most likely to have Lyra, Modern Health or Calm already.

6. **No lookalike, and no use of HubSpot history.** Medium to high impact over time. Lookalikes are deferred (pipeline.md:327, 351). HubSpot is used only to exclude. Spill's UK customer firmographics, US inbound demos, closed-lost US deals and the people who requested pricing could all set the ICP and raise the score. The weights today are untested priors (`defaults.py:154-240`).

7. **The learning loop is too slow and its attribution is biased.** Medium impact, because it caps how fast the system improves.
   - `rescore` deletes and rewrites every `signal_matched` row (`score.py:536-539`), so a 90-day signal disappears from accounts already sent (open-questions #33).
   - The Control baseline is "tier is Control now" (`02_v_signal_value.sql:67`), and that shifts when the Q4 points drop off.
   - "Reply" counts negatives and unsubscribes (`00_v_account_outcomes.sql:9`), and open reply windows are counted too.
   - Telling 1% from 2% positive replies takes about 2,000+ accounts per arm. *Fix:* freeze matches at enrolment, use positive and meeting rates, and count only closed windows.

8. **A noisy, risky industry definition.** Medium impact. Shared tech NAICS codes including 5415, keyword-only labels, and the Startups overlap all dilute fit and make the per-industry copy land on the wrong companies. Digital health and Healthtech let teletherapy competitors through (`tiers.py:56-84`). *Fix:* move the IT-services codes (541512, 541513, 541519) to exclude_naics, and add telehealth and teletherapy keywords to the partner list.

9. **Mis-sized hard exclusions, and gates that fail open.** Medium impact. "<5 US people" relies on Apollo's thin coverage. "Founded <2 years" removes funded young teams. Geography is checked only at HQ level (`tiers.py:200-229`). Missing facts pass every check.

10. **Free or cheap data left unused.** Medium impact. job_posts is not a source for the mental-health and EAP signals. `states_with_staff` has no signal. The Clay functions for tech stack (Slack/Teams, HRIS, PEO) and web traffic go unused.

11. **Data-hygiene gaps.** Low to medium impact.
    - Redirects are not followed at the door (`domains.py:184` has no caller).
    - The HubSpot check comes after the Clay spend, and two HubSpot exclusions are never written.
    - `catch_all_valid` is sendable while "risky contacts" is off.
    - Contacts carry no freshness fields.
    - Lower-case company names are kept as written.
    - Nothing enforces the re-contact rules.

12. **Clay budget versus volume.** Medium impact on volume, and indirectly on quality. Credits per account are unmeasured (`defaults.py:138`), and 2,000 a month likely supports about 100 accounts a week, not 150. Shrinking the budget by choosing a cheaper Claygent model would degrade page reads, which are the main source of score.

**The fastest wins before phase 1 code is written:**
- Widen the Roles titles.
- Fix the source of "Hiring and growth".
- Remove "EAP" and "employee assistance" from "Mental health support listed".
- Drop or rescale the values-page and Q4 signals.
- Give site visits an angle and Priority weight.
- Split the Hold list into direct competitors and complements.
- Add job_posts as a source for the mental-health and EAP signals.
- Plan a second contact per account at 50–249.
