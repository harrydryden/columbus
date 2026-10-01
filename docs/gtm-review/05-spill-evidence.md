# Spill's own evidence: which accounts and contacts convert

**Purpose.** This report sharpens the US outbound ICP, scoring and messaging (`/home/user/columbus`, SPEC sections 2, 5 and 9). The system's goal is 2x more interest per lead contacted.
**Prepared:** 1 Oct 2026. Read-only and aggregate only. Contacts are not identified; company names appear only in the short list of notable US customers in section 5.

---

## 0. Headline findings

| # | Finding | Evidence (n) | Confidence |
|---|---|---|---|
| 1 | **Win rate falls steadily with company size.** By employees covered: 1–9 won 53%, 10–49 won 44%, 50–99 won 28%, 100–249 won 24%, 250+ won 15%. US-HQ deals show the same shape: 10–49 won 33%, 50–99 won 28%, 100–249 won 11%. | 550 decided Spill 3.0 deals; 416 US-HQ deals | High (direction); medium (exact cut-offs) |
| 2 | **Larger customers stay longer and pay more.** Share still active: <10 = 28%, 10–49 = 35%, 50–99 = 42%, 250+ = 56%. Median won amount: about £90/month (1–9) up to about £350/month (100–249). | 782 companies; 207 won deals | Medium (no adjustment for cohort age) |
| 3 | **Seniority of the buyer matters more than function.** Founder/CEO/MD won 55%. Senior HR/People (Head, Director, VP, Chief) won 42% overall and 50% at 50+ employees. HR Manager, HR generalist or coordinator won 22–26%. Operations/office won 35%. | 357 contact-to-deal links | Medium–high |
| 4 | **Familiarity is the strongest source signal.** Buyers who used Spill before won 58–60%. Word of mouth overall won 49%. Organic, SEO or AI search won 31%. Paid ads won 16%. Legacy deals tagged "SDR – Outbound" won 15%. | 161–538 per group | High |
| 5 | **An existing EAP is a mild negative, not a positive.** Deals with an EAP already in place won 32%, versus 46% with nothing in place and 51% with only health insurance. About 26% of losses went to a competitor or to "we already have something". | 82 / 180 / 59 deals | Medium |
| 6 | **Industry: Tech and Creative Agencies are solid (39% each, highest volume). Legal is the standout small segment.** Law won 57% (n=14); Law was also the second-largest US customer cluster (10 of 52) with good retention (60% active). Healthcare (22%), Education (26%), Nonprofits (26%) and Construction (20%) are weak. | 576 deals with an industry | Medium (Legal is low-n) |
| 7 | **Speed separates winners.** Won deals: demo held a median 2.3 days after booking, and the next step came 7 days after the demo. Lost deals: 5.9 days to demo, then 56 days sitting at "Demo held". Deals that went to a trial won 63%. | Stage-time medians on 716 closed deals; 67 trial deals | Medium–high |
| 8 | **Spill already has US customer history.** 52 US-staffed customer accounts from 2020–22: Tech 20, Law 10, Creative 6, Nonprofit 5; median 20 covered staff; mostly NY and CA. 36 of the 52 have since churned. Recent US inbound from paid search (Q3 2026) is junk-heavy: 9 of 16 deals disqualified. | 62 US-HQ customer records; 22 US deals in Spill 3.0 | High (counts); low (relevance today) |
| 9 | **No cold-outbound reply or meeting rates exist in the sources I was allowed to use.** HubSpot sequences were mostly used on warm contacts. The only outbound outcome data is legacy deals tagged "SDR – Outbound" (15% win) and 86 SDR-sourced companies (13 became customers). | — | High (that the gap exists) |

---

## 1. Sources, definitions and caveats

**Sources used**

- **HubSpot portal 8481055**, read-only: `query_crm_data` aggregates, plus `search_properties`, `get_properties` and `search_crm_objects` for property discovery and one profile of US customers.
- **Pipelines**
  - **Spill 3.0** (82002613): 1,481 deals created 2021–2026, almost all from Q4 2023 onwards. This is the primary dataset.
  - **Legacy Sales Pipeline (pre-2024)** (`default`): 3,892 deals created 2020–2024. Used for US history and outbound history.

**Unavailable sources**

- **BigQuery `spill-warehouse-test`: not accessible here.** There is no BigQuery tool, no `bq` or `gcloud` CLI, no `google-cloud-bigquery` package and no credentials, so 0 GB was scanned. Question 4 (usage and retention) is answered with HubSpot proxies: subscription status, churn category and plan. Session uptake per employee **could not be measured**. Section 6 lists the queries to run when access exists.
- **Slack, Gmail, Apollo, Clay and Instantly were excluded by the brief.** So the archived #outboundsales channel was **not read**.

**Definitions**

- **Won** = Spill 3.0 "Closed won" (154381891) plus "Churned" (1287546140; 3 deals that were won and then churned).
- **Lost** = "Closed lost" (154381892).
- **Win rate** = won / (won + lost). Open deals are excluded.
- **Disqualified deals are excluded from all win rates.** They are reported separately:
  - "Not a real company" (156833161): 162 deals.
  - "Not OTS" (1276308130): 184 deals. Sampled lost-reason text shows these are individuals, therapists, competitors, 800+ employee organisations and requests for a benchmarking quote.
- **Spill 3.0 base:** 213 won, 503 lost, 30% win rate.
- **Deal amount** = `amount_in_home_currency` in GBP. It matches plan list prices (Starter about £90, Essential about £139), so it appears to be a **monthly fee**. Medians are of won deals only, because many lost deals have £0.
- **95% confidence intervals** are Wilson intervals, given as [low–high].

**Caveats**

1. **Missing data is not random.**
   - The deal field "Total number of employees" (`employees_covered__cloned_`) is filled on 204 of 210 won deals but only 343 of 503 lost deals.
   - Deal industry is filled on 205 of 210 won but 368 of 503 lost.
   - Both are captured at or after the demo: lost deals with a demo date (333) roughly equal lost deals with a size (343). **So size and industry win rates are effectively post-demo win rates.** The post-demo baseline is about 38% (208 / 541).
2. **"Employees covered" is what the buyer wants to cover, which can be smaller than total headcount.** The company's `numberofemployees` is HubSpot enrichment (global headcount, in coarse buckets such as 10, 25, 50, 250, 1000). Both measures are reported.
3. **Persona counts are contact-to-deal links, not deals.** A deal with two contacts counts twice. Persona is classified by keyword from job-title text (rules in section 3.5) and cross-checked against the HubSpot "Job Title (dropdown)" field.
4. **Country = HubSpot's company HQ country**, from enrichment. It is not where the staff work. The company "Geography" (`market`) field holds staff location as free text.
5. **Lost reasons are free text** (315 distinct values). They are bucketed by keyword, and the buckets overlap.
6. **"Subscription status" (Active/Churned) is a company-level flag that may be stale.** "Active share" is not adjusted for cohort age: older cohorts, such as 2020–22 Tech and Creative customers, have had longer to churn.
7. **Most evidence is UK inbound.** It shows what converts once someone raises their hand. It does **not** measure cold-email reply propensity, which is what the "2x interest per lead" goal is about.

---

## 2. Spill 3.0 pipeline overview

`SELECT pipeline, dealstage, COUNT(*) FROM DEAL GROUP BY pipeline, dealstage`

| Stage (Spill 3.0) | Deals |
|---|---|
| Demo requested | 67 (open) |
| Demo created | 36 (open) |
| Demo held | 170 (open) |
| On trial | 100 (open) |
| Onboarding | 46 (open) |
| Closed won | 210 |
| Churned (previously won) | 3 |
| Closed lost | 503 |
| Not a real company | 162 |
| Not OTS | 184 |
| **Total** | **1,481** |

**Legacy pipeline:** 867 won, 3,004 lost (22%), plus 21 in other stages.

**Win rate by creation year (Spill 3.0, closed only):** 2024: 32% (n=223); 2025: 39% (n=198); 2026 to date: 22% (n=261). The 2026 figure is understated, because 253 of its deals are still open, mostly at demo held, on trial or onboarding.

**Disqualification is rising in 2026.** "Not OTS" went from 31 deals in Q1 to 67 in Q2 and 69 in Q3. 98 of all 184 came from Google Ads.

---

## 3. Who wins: overall (Spill 3.0)

### 3.1 Company size

Two queries. First, by the deal's "employees covered" field:
`SELECT dealstage, COUNT(*), MEDIAN(amount_in_home_currency) FROM DEAL WHERE pipeline='82002613' AND employees_covered__cloned_ >= a AND employees_covered__cloned_ < b GROUP BY dealstage`
Second, the same bands using `COMPANY.numberofemployees`.
Active share query: `SELECT subscription_status, COUNT(*) FROM COMPANY WHERE subscription_status IS NOT NULL AND employees_covered in band`.

| Band | Won | Lost | n | Win rate [95% CI] | Median won amount (£/month) | Expected £/month per decided deal¹ | Active share (company, by covered staff) |
|---|---|---|---|---|---|---|---|
| 1–9 | 46 | 41 | 87 | **53%** [42–63] | 90 | 48 | 28% (44/158) |
| 10–49 | 111 | 144 | 255 | **44%** [38–50] | 135 | 59 | 35% (152/433) |
| 50–99 | 27 | 68 | 95 | **28%** [20–38] | 200 | 57 | 42% (47/113) |
| 100–249 | 16 | 50 | 66 | **24%** [16–36] | 347 | 84 | 40% (21/53) |
| 250+ | 7 | 40 | 47 | **15%** [7–28] | 510 | 76 | 56% (14/25) |

¹ Expected £/month per decided deal = win rate × median won amount.

**Robustness check (company enrichment headcount, n=462):**

| Band | Won / lost | Win rate |
|---|---|---|
| 1–9 | 13/20 | 39% |
| 10–49 | 44/93 | 32% |
| 50–99 | 47/75 | 39% |
| 100–249 | 8/31 | 21% |
| 250+ | 22/109 | 17% |

Both measures agree on a drop at 100+ employees. They disagree on 50–99: enrichment headcount includes staff outside the UK, so many "50–99 headcount" firms wanted fewer people covered.

**Reading.** Small teams close more often. Larger teams pay more and churn less, so expected value per decided deal is flat to rising up to 249 employees. Under 10 employees has the worst retention (28% still active).

### 3.2 Industry

Query: `SELECT industry, dealstage, COUNT(*), MEDIAN(amount_in_home_currency) FROM DEAL WHERE pipeline='82002613' AND industry IS NOT NULL GROUP BY industry, dealstage`. 576 decided deals have an industry.

| Website industry group (mapped from HubSpot deal industry) | Won | Lost | Win rate [95% CI] | Median won £/month | Active share (company, all-time)² |
|---|---|---|---|---|---|
| **Technology & Startups** (Tech, IT Services, Computer Games) | 68 | 105 | **39%** [32–47] | 175 (Tech) | Tech 41%; IT Services 65%; Games 57% |
| **Marketing & Creative Agencies** (Creative Agency) | 26 | 40 | **39%** [29–51] | 139 | 40% |
| **Professional Services** (Consultancy, Recruitment, Engineering, Architecture, Accounting) | 15 | 23 | **39%** [25–55] | 145–253 | Consultancy 41%; Recruitment 43% |
| **Legal Teams** (Law) | 8 | 6 | **57%** [33–79] | 275 | 60% (12/20) |
| Financial Services (Finance, Venture Capital) | 11 | 12 | 48% [29–67] | 90 | Finance 60%; VC 33% |
| Hospitality | 12 | 13 | 48% [30–67] | 135 | 52% |
| Manufacturing & Industrial | 12 | 11 | 52% [33–71] | 95 | 80% (12/15) |
| **Nonprofits** (NGO / Charity) | 14 | 40 | **26%** [16–39] | 139 | 55% (35/64) |
| Education (School) | 8 | 23 | 26% [14–43] | 199 | 6/8 |
| Retail & E-commerce (Retail, Direct to Consumer) | 8 | 20 | 29% [15–47] | 90 | DTC 55% |
| **Healthcare** | 8 | 28 | **22%** [12–38] | 119 | 0/1 |
| Construction & Trades | 5 | 20 | 20% [9–39] | 90 | 1/5 |
| Small businesses ("Mom & Pop") | 5 | 1 | 83% (n=6) | 195 | 56% |
| Other | 8 | 25 | 24% [13–41] | 90 | 41% |

² Active share query: `SELECT company_industry, subscription_status, COUNT(*) FROM COMPANY WHERE subscription_status IS NOT NULL GROUP BY 1,2`. n = 1,047 companies; 45% active overall.

**Caveat.** The baseline for this table is the 36% win rate of deals that have an industry, not the 30% overall. Read the rows relative to each other.

### 3.3 Country and US state

Query: `SELECT geography, dealstage, COUNT(*) …`, and `COMPANY.country` for HQ.

**By the deal's "Geography" field (Spill 3.0):**

| Geography | Won / lost |
|---|---|
| UK | 208 / 473 (31%) |
| Australia | 2 / 27 (7%) |
| USA | 2 / 2 |
| Europe | 0 / 1 |

**US-HQ companies:**
- Spill 3.0: 12 won / 27 lost (31%, [19–46]).
- Legacy: 71 won / 318 lost (18%, [15–22]), against 22% for all legacy deals.

**US state (legacy, HQ state; closed deals):**

| State | Won / lost | Win rate |
|---|---|---|
| NY | 17/89 | 16% |
| CA | 19/72 | 21% |
| TX | 5/10 | 33% |
| IL | 4/11 | 27% |
| VA | 4/9 | 31% |
| MA | 2/15 | 12% |
| FL | 2/12 | 14% |
| DE | 1/10 | 9% |
| PA | 2/4 | 33% |
| NJ | 0/8 | 0% |
| GA | 1/7 | 13% |

- The SPEC's active states (NY, MA, NJ, PA, IL, GA, TX) combined: 31/175 (18%).
- CA and WA combined: 21/103 (20%).
- **Reading: state does not separate winners from losers.** NY and CA are simply the largest pools.

### 3.4 Deal source

Three views of source. Query for the first: `SELECT hs_analytics_source, dealstage, COUNT(*) FROM DEAL WHERE pipeline='82002613' GROUP BY 1,2`.

**Original traffic source (Spill 3.0):**

| Source | Won | Lost | Win rate [95% CI] | Also disqualified |
|---|---|---|---|---|
| Organic search | 94 | 213 | 31% [26–36] | 66 |
| Direct traffic | 50 | 99 | 34% [26–41] | 68 |
| Offline (sales-created, imports, integrations) | 34 | 42 | 45% [34–56] | 70 |
| **Paid search** | 22 | 121 | **15%** [10–22] | **124** |
| Referrals (web) | 5 | 12 | 29% | 5 |
| AI referrals | 3 | 10 | 23% | 3 |

**How they heard about us (Spill 3.0, self-reported):**

| Grouped answer | Won | Lost | Win rate [95% CI] |
|---|---|---|---|
| **Word of mouth or prior user** (Recommendation 25/35; Buyer used Spill previously 25/17; Employee recommended 10/11; older options 8/8) | 68 | 71 | **49%** [41–57] |
| — of which "Buyer used Spill previously" | 25 | 17 | **60%** |
| Search, organic or AI (SEO 36/88; Organic 20/46; ChatGPT 9/14; Search engine 4/8) | 69 | 156 | 31% [25–37] |
| **Paid ads** (Google 24/120; Bing 2/15) | 26 | 135 | **16%** [11–23] |
| Partners (CharlieHR, Iwoca, OpenOrg) | 2 | 4 | 33%; plus 23 open, including 15 CharlieHR deals on trial |
| Outbound | 1 | 0 | plus 3 open (1 demo held, 2 on trial) |
| Other | 20 | 26 | 43% |

**Deal Source field.** Spill 3.0: inbound 137/389 (35%); "other" 16/61 (26%). There is no outbound value. **Brokers are not recorded anywhere** as a source.

Legacy outbound comparison is in section 7.

### 3.5 Persona of the main contact

Query: `SELECT jobtitle, DEAL.dealstage, COUNT(*) FROM CONTACT WHERE DEAL.pipeline='82002613' AND jobtitle IS NOT NULL GROUP BY 1,2`. This covers 129 won and 228 lost contact-to-deal links. Titles were then bucketed by keyword:
- **Founder/CEO** = founder, CEO, owner, MD, managing director, president, partner, GM, executive director.
- **HR/People** = people, HR, talent, culture, wellbeing, DEI, reward, benefits, recruit. **Senior** if the title also contains head, director, chief, VP, lead, senior, global or group.
- **Operations** = operations, COO, chief of staff, office, admin, EA/PA, studio or practice manager.
- **Finance** = finance, CFO, controller, accountant.

| Persona | Won | Lost | n | Win rate [95% CI] | At 50+ covered staff | At <50 or unknown |
|---|---|---|---|---|---|---|
| **Founder / CEO / MD / GM** | 37 | 30 | 67 | **55%** [43–67] | 56% (n=9) | 55% (n=58) |
| **HR/People, senior** (Head, Director, VP, Chief People) | 28 | 38 | 66 | **42%** [31–54] | **50%** (n=30) | 36% (n=36) |
| Operations / office / EA | 25 | 46 | 71 | 35% [25–47] | 25% (n=8) | 37% (n=63) |
| **HR/People, manager or junior** (HR Manager, generalist, advisor, coordinator) | 23 | 76 | 99 | **23%** [16–32] | **22%** (n=60) | 26% (n=39) |
| Finance | 3 | 7 | 10 | 30% | — | — |
| Other | 13 | 31 | 44 | 30% | — | — |

**Cross-check with the HubSpot "Job Title (dropdown)" field** (`SELECT CONTACT.job_title, dealstage, COUNT(*) FROM DEAL …`):

| Dropdown value | Won / lost | Win rate |
|---|---|---|
| Directors / Founders | 31/25 | 55% |
| Senior HR or Ops | 17/12 | 59% |
| Junior HR or Ops | 28/49 | 36% |
| Other role | 23/40 | 37% |

The plain "HR Manager" title alone: 3 won / 8 lost overall; 1 won / 6 lost at 50+ staff.

### 3.6 Existing support and buying trigger

**Existing support** (deal field, recorded at demo):

| Existing support | Won | Lost | Win rate [95% CI] |
|---|---|---|---|
| **Nothing** | 82 | 98 | **46%** [38–53] |
| **Health insurance** | 30 | 29 | **51%** [38–63] |
| **EAP** | 26 | 56 | **32%** [23–42] |
| Mental Health First Aiders (MHFA) | 8 | 7 | 53% |
| Wellbeing platform | 6 | 8 | 43% |
| Corporate therapist | 7 | 3 | 70% |

**Current problem (buying trigger)**, from the multi-select dropdown recorded at demo. The baseline for deals with a trigger recorded is about 50%.

| Trigger | Won / lost | Win rate |
|---|---|---|
| Coverage gap (locations, time zones, staff outside insurance) | 13/5 | **72%** |
| Crisis (for example a bereavement) | 15/6 | **71%** |
| Growth or scaling the team | 13/8 | 62% |
| Manager capability (issues landing on HR or directors) | 17/11 | 61% |
| Nothing in place at all | 25/18 | 58% |
| Employees asked for support | 13/11 | 54% |
| Duty of care / being proactive | 27/25 | 52% |
| Emotionally demanding work | 26/24 | 52% |
| Have someone struggling | 43/44 | 49% (the most common trigger) |
| Incumbent contract ending | 14/15 | 48% |
| Incumbent not working (unused, capped, hard to access) | 30/34 | 47% (second most common) |
| Benefits review | 15/23 | 39% |
| Employee survey results | 7/11 | 39% |
| Cost pressure | 4/9 | 31% |
| **New HR or People leader reviewing provision** | **2/8** | **20%** |
| Redundancies or restructuring | 7/1 | 88% (n=8) |

---

## 4. Speed and leakage (Spill 3.0)

Queries:
- Cycle time: `SELECT dealstage, MEDIAN(days_to_close) …`.
- Stage time: `MEDIAN(hs_v2_cumulative_time_in_<stage_id>)`. HubSpot labels these "seconds", but the values are milliseconds; they are converted to days here.
- Demo held: `demo_held_date IS NOT NULL`.

**Time in stage (median):**

| Stage | Won deals | Lost deals |
|---|---|---|
| Demo requested → Demo created | about 4 minutes (auto-advanced) | about 11 minutes |
| **Demo created → Demo held** (booked to held) | **2.3 days** | **5.9 days** |
| **Demo held → next stage** | **6.9 days** | **55.9 days** (lost deals sit at "Demo held" before being closed) |
| On trial | 74 days | 97 days |
| Onboarding | 15 days | 73 days |
| **Create → close** | **39 days** (mean 89); 25 days without a trial; 104 days with a trial | **82 days** (mean 158) |

**Funnel of closed, qualified deals (n = 716):**

| Step | Deals | Rate |
|---|---|---|
| Demo held | 541 | 76% |
| Lost before a demo | 170 | 24% (90 of them ghosted, cancelled or no-show) |
| Won after a demo held | 208 of 541 | 38% |
| Went through a trial | 67 | **63% won** [51–73] |

**Biggest leaks, in deal count:**
1. **Post-demo ghosting**: 98 of 333 post-demo losses (29%).
2. **Pre-demo drop-off**: 170 deals (24% of closed).
3. **Chose a competitor after the demo**: 49.
4. **Price after the demo**: 34.

**Top of funnel.** 23% of all Spill 3.0 deals were disqualified (not a real company, or not OTS), and the share is concentrated in paid search.

---

## 5. US-specific

**1. US companies in HubSpot: 1,579 with HQ country = United States.** The `hs_country_code` field is filled on only 216 of them, so use `country`.

| View | Breakdown |
|---|---|
| Lifecycle | Lead 1,171; Opportunity 231; **Customer 59; Churned 3**; unassigned 115 |
| How the record was created | 97% auto-created from contact email domains (CRM_SETTING 1,533); CRM UI 39; import 3; automation 4 |
| Original source | Organic search 782; Direct 275; Offline 275; Referrals 126; Paid search 55; Social 14; other 13 |
| Staff geography (`market` field) says US-only | 234 companies ("united states" 209, "us" 15, "usa" 10): 48 customers, 5 churned, 166 opportunities |

**2. US deals.**
- Legacy pipeline: 389 closed US-HQ deals, 18% won.
- Spill 3.0: 85 US-HQ deals: 12 won, 27 lost, 30 disqualified, 16 open.
- **Deals flagged geography = USA (Spill 3.0): 22.** 19 were created Q2–Q4 2026. 16 came from Google Ads; 9 of those were disqualified and 7 are still open (0 won so far). The 2 US wins have no source recorded.

**3. US win rate by company size, both pipelines, HQ in the US.** Query: `SELECT dealstage, COUNT(*) FROM DEAL WHERE COMPANY.country='united states' AND COMPANY.numberofemployees in band GROUP BY dealstage`.

| Band | Won | Lost | Win rate [95% CI] |
|---|---|---|---|
| 1–9 | 1 | 1 | n=2 |
| 10–49 | 22 | 45 | **33%** [23–45] |
| 50–99 | 31 | 78 | **28%** [21–38] |
| 100–249 | 4 | 33 | **11%** [4–25] |
| 250+ | 23 | 178 | **11%** [8–17] |

**4. US customers: profile.** 62 US-HQ companies with lifecycle Customer or Churned. All were created Nov 2020 to Oct 2022, in the legacy era. 10 of them have staff outside the US (UK, NZ or Australia) and are excluded, leaving **52 US-staffed accounts**.

| Dimension | Breakdown (n=52) |
|---|---|
| Industry | **Tech 20; Law 10; Creative agency 6; Nonprofit 5**; Games 2; Consultancy 2; IT services 1; Recruitment 1; DTC 1; unknown 4 |
| Covered staff | 1–9: 14; **10–49: 27**; 50–99: 7; 100–249: 3; 250+: 1. Median covered = **20** |
| HQ headcount (enrichment) | 10–49: 15; 50–99: 23; 100–249: 3; 250+: 11 |
| State | **NY 14; CA 11**; IL 5; TX 4; VA 3; CT, FL, NC and PA 2 each; 1 each in OK, WA, MN, MD, MA, OH and GA |
| Source | Direct 19; Referrals 17; Offline 6; Organic 5; Paid search 4 |
| How they heard | Search engine 11; **Recommended 10**; had Spill at a previous company 2; content 2; social 2; SDR outbound 1; blank 23 |
| Status today | **Active 15; Churned 36**; blank 1. Active share is highest at 10–49 covered staff (10 of 27) |
| Median recent deal amount | 277 (currency field not recorded; probably USD) |

**Notable US customers** (US-staffed, still flagged active; the flag may be stale): seven companies, mostly tech, with one legal firm and one tech nonprofit, in TX, NY, CA, MA and MN. Names are left out of the repository; in HubSpot they are the companies with country = United States and subscription status Active.

---

## 6. Usage, retention and expansion

**BigQuery was not available**, so employee session uptake by segment could not be measured.

**HubSpot proxies:**
- **Active share by size** (section 3.1) rises with covered headcount: 28%, 35%, 42%, 40%, 56%.
- **Active share by industry** (section 3.2) is highest in IT Services (65%), Law (60%), Finance (60%), Games (57%), Nonprofit (55%) and DTC (55%). It is lowest in Tech (41%), Creative (40%) and Consultancy (41%). The Tech and Creative cohorts are the oldest, so age effects are likely.
- **Recorded churn category** (only 20 companies have one): low usage 12; private healthcare 4; going out of business 2; cutting costs 1; dissatisfaction 1. **Low usage is the main recorded churn driver.**
- **Won plan mix (Spill 3.0, n=210):** Essential 64; Starter 38; Core EAP + Treatment 35; Core EAP 29; Team 18; unassigned 24.

**Queries to run when BigQuery access exists** (each under 1 GB):
1. `views.monthly_sessions` by `company_id`, sessions per covered employee, joined to `postgres_accounts.company` size and industry.
2. `ledger_views.v_company_billable_balance_monthly` for net revenue retention by cohort.
3. `postgres_billing_and_usage.trial` against conversion.

The aim is to identify the size and industry segments with the highest sessions per employee. Those are the best lookalikes for the "30% of employees use Spill" proof point.

---

## 7. Past outbound

| Evidence | Finding |
|---|---|
| **Legacy deals "how they heard" = SDR – Outbound** | **12 won / 66 lost = 15%** [9–25]. Compare search engine 22%, social 27%, recommended 38%, had Spill at a previous company 58%. |
| Other legacy SDR tags (SDR ghosted, SDR marketing follow-ups, SDR outreach to free-trial users) | 7 won / 65 lost = 10% |
| Legacy "Mental health talk" leads (event-sourced) | 7 won / 89 lost = **7%** |
| **Companies tagged "SDR – Outbound"** | 86 companies, all created 2020–22: **13 customers (15%)**, 55 opportunities, 15 leads, 3 unassigned. **US subset: 22 companies, 1 customer, 11 opportunities.** |
| Spill 3.0 "Outbound" source | 4 deals: 1 won, 2 on trial, 1 at demo held |
| HubSpot sequences | **2,800 contacts enrolled.** By year: 2021: 332; 2022: 312; 2023: 1,652; 2024: 86; 2026: 418. **Mostly warm**: by how the contact was created, form 1,179, meetings 552, integration 347, extension 314, import 258. By lifecycle, 924 are customers and 712 opportunities. 52% have any sales-email reply. These are onboarding and nurture sequences, not cold outbound, so **no cold reply rate can be derived.** |
| Contact lead status | Barely used for outbound: "Deal closed" 3,402; New 201; every other value 3 or fewer. "Attempted to contact" and "Connected" are unused. |
| Company "Target account" flag | Not used |
| #outboundsales Slack channel | **Not read**: Slack is excluded by the brief |

**Bottom line.** Reply and meeting rates from past outbound are **not recorded in HubSpot**. The recorded outcome is that outbound-sourced deals closed at about 15%: half the rate of search inbound, a third of word of mouth, and the same as paid ads.

---

## 8. Lost reasons and objections

503 Spill 3.0 lost deals, all with free-text reasons. Each bucket is one `COUNT(*)` with `closed_lost_reason LIKE …` patterns. The buckets overlap.

| Bucket (patterns) | Lost deals | Share | Of which after a demo |
|---|---|---|---|
| Ghosted / no response (ghost, no response, no reply, radio silence, went cold) | 130 | 26% | 98 |
| Cancelled or no-show before the demo (cancel, attend, no show) | 65 | 13% | — (90 pre-demo losses were ghosted or cancelled) |
| **Chose a competitor or alternative** (another provider, alternative, went with, gone with, competitor) | 75 | **15%** | 49 |
| **Already has an EAP or insurance** (" eap", bupa, insurance, already have, health assured) | 54 | **11%** | — |
| **Price / budget** (cost, price, budget, expensive, afford, cheaper) | 44 | **9%** | 34 |
| Priority / timing (priorit, timing, next year, postpone, on hold) | 44 | 9% | — |
| Approval / champion left (left, board, CEO, sign off, approv) | 28 | 6% | — |
| Product fit: format (Slack, Teams, coaching, app, perks, benefits platform) | 25 | 5% | — |
| Doubts about usage (usage, uptake, use it, engagement, holistic) | 17 | 3% | — |
| Geography / coverage (Australia, global, countries) | 12 | 2% | — |

**Recurring themes in the text:**
- "we already have an EAP / Bupa / Health Assured"
- "no uptake with current provider, want a different approach"
- "wanted a broader or holistic offering (coaching, benefits bundle)"
- "cheaper provider or bundled perks platform"
- "board said no"
- "didn't use Slack or Teams"
- "contact left"

**Churn reasons** (company level, n=20): low usage dominates.

---

## 9. Implications for the US ICP, scoring weights, persona choice and messaging

Confidence key: **High** = consistent across measures with large n and separated intervals. **Medium** = consistent but moderate n, or a known bias. **Low** = small n or indirect.

### ICP (size, industry, geography)

1. **Weight the 10–99 band ahead of 100–249.** Change the enrolment tie-break from "20 to 99 first" to **10–49 first, then 50–99, then 100–249**.
   - Win rate falls 44% → 28% → 24% (UK), and 33% → 28% → 11% (US).
   - 100–249 accounts are still worth contacting: they have higher value and better retention (40–56% active). But require **score ≥ standard_threshold and a senior People leader as the contact** before enrolling them.
   - Confidence: **High** on direction; Medium on the exact cut.
2. **Keep the floor at 10 employees and the "fewer than 5 US-located people" exclusion.** Under-10 accounts close often but pay about £90/month and have the worst retention (28% active). **High.**
3. **Industry order is broadly right.** Keep Technology & Startups (priority 1) and Marketing & Creative Agencies (priority 2), both at 39%, the highest volumes, and 26 of the 52 US customers.
   - **Move Legal Teams forward**, from January to the first wave, or at least first in January. Law won 57% (n=14), was the second-largest US customer cluster (10 of 52, small firms spread across NY, IL, CT, TX, VA, MN and FL), has 60% active share and the highest median won amount (£275). **Medium** (small UK n, but corroborated by US history).
4. **Nonprofits: keep for January but rank below Legal and Professional Services.** They win 26%, though they retain well (55%) and had 5 US customers. **Medium.**
   - Professional Services won 38%; keep them after January as planned.
   - Keep Healthcare (22%), Education (26%) and Construction (20%) **off**. **Medium.**
   - Manufacturing (52%, n=23), Hospitality (48%, n=25) and Financial Services (48%, n=23) did better than expected in the UK. They are candidates for a later test, not v1. **Low.**
5. **State does not predict winning** (NY 16%, CA 21%, TX 33% on small n, active states 18%). Choose states for operational and compliance reasons, not conversion. Excluding CA and WA gives up the second-largest historic US customer pool (12 of 52 customers). If that exclusion is a compliance decision, keep it, but record it as a known cost. **Medium.**

### Persona choice (SPEC 5, Roles)

6. **10–49 staff: founder or executive first.** This is confirmed: founders/CEOs/MDs win 55% (n=58), against about 37% for Operations and senior HR. The fallback can stay Operations. **Medium–high.**
7. **50–249 staff: People leader first, but only senior titles.** Senior HR/People won 50% at 50+ (n=30). HR Manager, generalist and advisor titles won 22% (n=60).
   - **Remove "HR Manager" from the People-leader title list** (or move it below the founder), and add an explicit seniority filter: Head, Director, VP or Chief People, or People & Culture Lead/Director.
   - **Swap the fallback order to People leader → Founder or executive → Operations.** Founders won 56% at 50+ (n=9) and 55% overall; Operations won 25% at 50+ (n=8).
   - Confidence: **Medium** on seniority; **Low–medium** on the swap.
8. **Keep "Finance is not contacted"**: finance contacts won 30% (n=10). **Low–medium.**

### Scoring weights (SPEC 5, Signals)

9. **Add a signal: "former Spill admin or user now at this company"** (from HubSpot contacts with a past Spill-customer association, plus job changes). Give it about **+30** and the "familiar" angle. Buyers who had used Spill won 58–60%, the highest of any segment; word of mouth won 49%. Volume will be small, but it is the clearest conversion lift in the data. **High** on conversion; low on volume.
10. **Cut "EAP named" from +10 to 0–5.** Keep it only as the trigger for the "Upgrade the EAP" angle, and keep the angle. Prospects with an EAP won 32%, against 46% with nothing in place, and incumbents or competitors account for about 26% of losses. The "Mental health support listed" (+25) signal cannot be validated from CRM data, so put it in the first A/B readout. **Medium.**
11. **Cut "New People leader" from +30 (the heaviest signal) to about +15 until outbound data justifies more.** The CRM trigger "New HR or People leader reviewing provision" won only 2 of 10. Small n, and reply behaviour may differ, so treat it as a test. **Low.**
12. **Growth signals hold up.** The "Growth or scaling the team" trigger won 62% (n=21), which supports Hiring +15 and Funding +20. **Low–medium.**
13. **Add a size signal**, from Apollo or Clay headcount: 10–49 → +15; 50–99 → +10; 100–249 → 0. This puts recommendation 1 into the score rather than only the tie-break. **Medium.**
14. **Plan expectations.** Outbound-sourced opportunities historically closed at about 15%, the same as paid ads. Assume about 15–20% of meetings convert, not the 30% inbound average. For the stop rule (5 meetings per 1,500 accounts), that means about one customer. **Medium.**
15. **Website-visit signals need a firmographic gate.** In Q3 2026, 9 of 16 US paid-search deals were junk (individuals or very large organisations). Only score US site visits from domains that pass the size and industry filters. **Medium.**

### Messaging (SPEC 5 Angles, SPEC 10 Copy)

16. **Lead with the triggers that close.**
    - **Coverage or access gap** (72%): "your plan technically covers therapy, but deductibles, networks and waitlists mean people don't use it". In the US this is the natural complement to the health-insurance-only group, which won 51%.
    - **Manager capability** (61%): "when issues land on you or your managers".
    - **Someone struggling or a crisis** (49% and 71%; this is the most common trigger).
    - **Growth** (62%).
    - **Medium.**
17. **Reframe "Upgrade the EAP" around under-use.** Lead with "unused, capped, hard to access" (64 deals, 47% won), not "you take this seriously". Use proof of usage ("30% of employees use Spill") and the absence of caps. Expect "we already have an EAP" (11% of losses) and competitor alternatives (15%). Price is a minor objection (9%), so the flat fee from $195 can be stated plainly and need not be the headline. **Medium.**
18. **Founders: duty of care and growth (52% and 62%). Senior HR: usage and benefits review.** Benefits review is weaker (39%), so pair it with a trigger. **Low–medium.**
19. **Ask for meetings fast and follow up fast.**
    - In-copy CTA and booking page should offer **slots within 48 hours**: won deals met in 2.3 days, lost deals in 5.9.
    - After the demo, set the next step **within 7 days**: won 6.9 days, lost 56 days.
    - **Default to a trial**: 63% of trials win.
    - This fits the SPEC's 20-minute reply handling.
    - **Medium–high.**
20. **Avoid soft, event-style calls to action** (talks or webinars) as the primary ask: legacy talk-sourced leads won 7%. **Medium.**
21. **Measure what HubSpot never captured.** Log reply rate, positive-reply rate and meetings per 100 accounts by size band, persona, industry and angle. This produces the baseline that "2x interest per lead" needs, because Spill has no historic cold-reply data. **High** that the gap exists.

---

**Tools and cost.** HubSpot read-only aggregates only: about 80 `query_crm_data` and property calls, plus one 62-record `search_crm_objects` profile of US customers. BigQuery: 0 GB scanned (not available). Nothing was created, updated or deleted.
