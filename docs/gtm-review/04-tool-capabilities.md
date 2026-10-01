# US Outbound: what the tools can do vs what the system uses

Prepared 1 Oct 2026, from the repo (`/home/user/columbus`) plus web research and read-only account checks. Repo paths are relative to `us_outbound/`.

**The short version.** The system uses each tool for its narrowest job: Apollo for search and email reveal, Clay for two functions, Instantly for sending and reading replies, HubSpot for warm-lead writes, and Claude for structured JSON. The biggest unused levers that need no new paid service are:
- acting the same day on visits to `/us` (Apollo's visitor filters);
- naming real US customers as proof (they are already in HubSpot);
- timed "why now" filters in Apollo, plus Form 5500 renewal dates;
- a cheap per-account opener written by Claude using the Batch API;
- spending Apollo credits that will otherwise lapse on 21 Aug 2027.

## 0. Read-only checks (1 Oct 2026, no credits spent)

| System | Finding |
| :- | :- |
| Apollo (`users_api_profile`, `credit_usage_stats`) | **Lead credits:** 30,188 left of 30,440. **AI credits:** 3,000,000, none used. **Inbound website-visitor credits:** 1,200. **Direct-dial credits:** 30,000 (the two tools disagree on whether they are used). **Dialer:** 2,880 minutes. **Waterfall:** email and phone both enabled. **Cycle:** 21 Aug 2026 to 21 Aug 2027 |
| Clay (`get-current-workspace`, `list_subroutines`) | Workspace "Spill" (1336346). Only four functions: Work Email, Company Latest Funding, Website Technology Stack (BuiltWith), Website Traffic. The two US Outbound functions don't exist yet. The plan tier isn't exposed |
| HubSpot (`get_organization_details`, property search, counts) | **Account:** STANDARD (the seat list was blocked by permissions). **Buyer-intent properties:** they exist (`hs_intent_signals_enabled`, `hs_recent_intent_signals`, `hs_intent_visitors_last_30_days`, `hs_is_target_account`), but **0 companies have intent tracking on**, and the connection lacks `buyer_intent.read`. **Customer companies:** 713, of which **59 have country = United States**. **Closed-lost deals:** 3,858 |
| Webflow (site head code) | The Apollo tracker loads only on paths starting `/us`. Google Tag Manager (GTM-PD34VWS) is site-wide. There's no direct HubSpot script, so whether HubSpot tracking runs through GTM is unverified |

## 1. Apollo

**Used today**
- `clients/apollo.py:31-40` is the only endpoint table:
  - `POST /usage_stats/credit_usage_stats` (0 credits);
  - `POST /mixed_companies/search` (1 credit a page);
  - `GET /organizations/enrich` (1 credit);
  - `POST /mixed_people/api_search` (0 credits);
  - `POST /people/bulk_match` (credits per reveal);
  - `GET /website_visitors/domain_aggregates` (path not yet confirmed), called once per organisation (`:187-216`).
- `:44-49` forces no personal emails, no phones and **no waterfall**. The stated reason is that waterfall results "only arrive by webhook".
- The planned filters are locations, employee ranges, NAICS and keywords, `q_organization_job_titles`, `organization_num_jobs_range` and person titles (SPEC 7; `docs/pipeline.md:54-57`).
- No source module is built yet (only `sources/named.py`).

**High-value capabilities not used**

| Capability | On current plan? | Cost | Concrete use here |
| :- | :- | :- | :- |
| **Visitor filters on company search**: `website_visitors_from_domains`, `_intent` (low/med/high), `website_visitors_domain_pages`, `web_page_view_counts`, `show_new_companies_only`, `website_visitors_from_past` (1 to 90 days) | Yes. The tracker is live on /us; 1,200 visitor credits | 1 credit a page of 100 (or 0, see next row) | One daily call finds every visiting company, including unknown ones. That replaces N per-organisation aggregate calls and settles the SPEC 7 discovery question |
| **Free Organization Lookup** with the same discovery filters: department headcount, headcount growth over N months, job titles plus `organization_job_posted_at_range`, technologies, funding, revenue | The MCP tool documents it as free. Its REST path needs confirming (PHASE0-CONFIRM) | 0 | "Tag sweeps" that set signals without buying search pages |
| Job-posting dates (`organization_job_posted_at_range`) and growth windows (`organization_headcount_growth_past_n_months`) | Yes | 0 to 1 a page | Dated triggers such as "first People role posted in the last 30 days" and "grew 15% in 6 months" |
| Technographic filter (`currently_using_any_of_technology_uids`) | Yes | 0 to 1 a page | A Slack or Teams user is a natural fit for a Slack-native service. A PEO or HRIS user (Justworks, TriNet, Gusto, Rippling) probably has an EAP bundled in |
| People search filters `contact_email_status` (verified) and seniority | Yes | 0 | Choose candidates Apollo already holds a verified email for, so each reveal lands and Clay is needed less |
| **Waterfall email, polled.** The MCP's `webhook_result_show` polls by `request_id` and keeps results for 30 days | Enabled | Variable; 0 when Apollo's own data answers | Contradicts `apollo.py:42-43`. Catch-alls and misses could be settled in Apollo before Clay is used |
| Organization Job Postings / News Articles | Yes | 1 credit a request or page | Posting text where there's no public ATS board; news hooks (awards, new office, acquisition) for Priority accounts |
| Buying intent (Bombora and LeadSift topics, company level) | In the app UI only. It isn't in the API or MCP search schema. Basic plans get about 6 topics, Organization about 12 | Included | Harry exports a weekly CSV. Thin coverage for 10 to 249 employees |
| AI credits (3M): AI fields and research agent | Yes, but only through collections and custom fields, which are **writes** | AI credits only | Free per-account research. Blocked by "Apollo is read only"; needs a narrow exception |
| Phones and dialer | Waterfall phone enabled; direct-dial pool unclear | Direct-dial credits | A call task for Priority accounts. `apollo.py:44-49` bans phones and humans have under 3 hours a week, so this is deferred |

**Shortlist:**
1. Visitor filters, daily.
2. Free lookup tag sweeps (dated jobs, growth, Slack or Teams, PEO).
3. People search filtered to verified emails.
4. Polled waterfall.
5. News for Priority accounts only.

**Budget fact.** At 2,000 a month, about 8,800 credits remain at the 21 Aug 2027 reset, and they lapse. That is about 3,800 above `apollo_floor`. Spending the excess on items 3 to 5 costs nothing extra; it is Harry's call (`pipeline.md:150-158`).

## 2. Clay

**Used today**
- `clients/clay.py:129-135` calls `POST /public/v0/routines/function:t_…/run`, and `:174-180` polls `GET /routines/run/{id}/results`.
- `:201-242` is the CSV fallback; `:295-380` validate the output.
- Only "US Outbound – Accounts" (Claygent page reader, clean name, funding) and "US Outbound – Contacts" (the Work Email waterfall) are planned.

**High-value capabilities not used**

| Capability | On plan? | Cost | Use here |
| :- | :- | :- | :- |
| **Pricing 3.0 meters** (11 Mar 2026): Data Credits (from $0.05) and Actions (under $0.01; 15k on Launch, 40k on Growth) | Applies if the workspace is on current pricing | n/a | `credits_used` and `clay_monthly_credits` track one number. Actions need their own budget line |
| Public API and Routines, "on every plan" since 9 Jul 2026 | Yes | 1 Action plus data per step | Removes the CSV-fallback worry (`phase0-facts.md` "Partly") |
| **More fields from the same Claygent run**: PEO or broker named, Slack or Teams mentioned, plan-year or open-enrollment month, awards (GPTW, B Corp), states hired in | Yes | About 0 extra (same task; flat price for native models) | Better openers and timing at no new per-row cost |
| Existing **Website Technology Stack** function (BuiltWith) | In the workspace, unused | Data credits | Detect the ATS or HRIS (BambooHR, Gusto, Rippling) and the careers platform. Run only where Apollo has no technology data |
| Signals: Job Change, New Hire (1 Action plus 0.2 DC per check), News & Fundraising, Job Posting; Web Intent (Growth; duplicates the Apollo tracker) | Launch and above | As stated | Job Change on UK customer champions who move to US firms. The others duplicate free Apollo data |
| Find People with role fallbacks | Yes | Data credits | Redundant: Apollo people search is free |

**Shortlist:**
1. Extend the Claygent prompt.
2. Split the budget into DC and Actions.
3. Job Change on champions (phase 4).
4. Tech Stack only as a fallback.

## 3. Instantly

**Used today (`clients/instantly.py`)**

| Area | Endpoints (line) |
| :- | :- |
| Accounts | `GET /accounts/{email}` (:312); `POST /accounts/warmup-analytics` (:339); `POST /accounts/warmup/enable` (:378); `GET /accounts/analytics/daily` (:403); `PATCH /accounts/{email}` (:420) |
| Campaigns | list, get, create, patch, pause and activate (:437 to :533); `GET /campaigns/{id}/sending-status` (:542); `GET /campaigns/analytics/steps` (:561) |
| Leads | `POST /leads/add` (:593); list (:626); get (:644); delete (:652) |
| Emails | list (:684); get (:703); `POST /emails/reply` (:720); `POST /emails/forward` (:747) |
| Blocklist | `POST /block-lists-entries/bulk-create` (:765) |

The settings at `:41-65` are: stop on reply and stop for company on; tracking off; `text_only` follows `email_format`; one variant per step; four steps seven days apart.

**High-value capabilities not used**

| Capability | On plan? | Cost | Use here |
| :- | :- | :- | :- |
| `first_email_text_only` (campaign field) | API v2 (Growth plan and up) | Free | Step 1 goes as plain text, steps 2 to 4 as HTML. SPEC 10 wanted step 1 plain anyway, and it helps inbox placement |
| ESP or provider matching; webhooks (Hypergrowth); custom tracking domain | Campaign option / plan | Free | ESP matching only if the registry mixes providers; webhooks and the tracking domain aren't needed (the system polls; tracking is off) |
| **Subsequences** and moving a lead into or out of one by API; status, activity and keyword triggers | API v2 | Free | Visit-triggered or signal-triggered branches inside "US Outbound – …" campaigns |
| `POST /leads/update-interest-status` (Interested, Meeting Booked, Wrong Person and so on) | API v2 | Free | Write the classifier's verdict back, so Instantly's analytics and the shared Unibox don't contradict Postgres |
| AI custom reply labels (Unibox NLP) | Included | Free | A second opinion on Claude's classification; saves Claude spend on obvious out-of-office replies |
| Inbox placement tests (`/api/v2/inbox-placement-tests`, plus analytics) | **Paid add-on**: $47 a month one-off tests, $97 a month automated | Paid upgrade | Automates the four seed-inbox checks and feeds the kill rule |
| A/Z variants with auto-optimise | Included | Free | **Don't.** It conflicts with SPEC 12 (one hash-split test; nothing re-weights itself) |
| AI Reply Agent (5 credits a reply; human-in-the-loop mode) | Credits | Paid | Conflicts with Slack approval (SPEC 1.3) |
| Website Visitors pixel (US only, person-level) | $97+ a month | Paid | Conflicts with "contact-level tracking off" |

**Shortlist:**
1. Subsequences driven by visits and not-now dates.
2. `first_email_text_only`.
3. Write back interest status.
4. Inbox placement API as a paid option ($47 to $97 a month).

## 4. HubSpot

**Used today (`clients/hubspot.py`)**
- CRM v3 search (:128, :206).
- Get (:138), pipelines (:158), owners (:168), properties and groups (:181, :186).
- Create and patch (:252, :271); v4 associations (:294).
- Notes, tasks and deals (:305, :329, :346); property group and property creation (:354, :363).
- Unsubscribe (:378) and GDPR delete (:388).
- Meetings readback is planned (`crm/readback.py` isn't built).

**Probable tier.** Custom deal pipelines and the Customer Success Workspace property (`hs_current_customer`) suggest Sales or Service Hub **Professional** or higher. This is unconfirmed.

| Capability | On plan? | Cost | Use here |
| :- | :- | :- | :- |
| **Customer and lost-deal records**: 713 customers (59 US), 3,858 closed-lost deals | Yes. Reads are allowed | Free | Named **US** proof points by industry; seeds for lookalikes, which Harry deferred; closed-lost reasons to inform objection lines |
| Buyer intent: visitor intent, research intent, custom signals (shipped 30 Jun 2026) | Pro+; 10 HubSpot credits per tracked company per month (Pro includes 3,000 a month) | Credits | **Excluded.** It needs cold companies in HubSpot, which breaks "warm leads only" |
| Contact analytics (`hs_analytics_*`) after a positive reply | Yes | Free | Pages a warm lead viewed, for Harry's demo prep, in the Slack alert |
| Lead scoring, sequences, Breeze Prospecting Agent ($1 a lead) | Pro | Included or credits | Not used: Python owns scoring; Instantly owns sending |

**Shortlist:**
1. US customer proof.
2. Closed-lost objection mining for the readout.
3. Analytics in the warm-lead alert.

## 5. Claude API

**Used today**
- `clients/claude.py:200-206` makes one `messages.create` call with `output_config.format` (json_schema).
- The prices at `:43-48` match the published ones: Haiku 4.5 $1/$5; Sonnet 5.5 $2/$10; Opus 5.5 $4/$20 and cache reads $0.20.
- There's a month ledger cap (`:184-193`).
- Callers: `copy_draft` (Opus, 16k max tokens; `enrol/copy_desk.py:379-381`) and `copy_qa` (Sonnet, 3k; `:323-325`). Reply classification is planned for phase 2.
- **Not used:** Batch, prompt caching, effort control, server tools, Haiku.

**Gaps that matter now**
- **Thinking is billed as output.** Sonnet 5.5 and Opus 5.5 run adaptive thinking by default (effort `high` on Sonnet). `claude.py` sets no `effort`, so a 1,024-token classification can hit `max_tokens` and pay for thinking. Set `output_config.effort: "low"` for classification and QA, or use Haiku 4.5.
- **Batch API: 50% off** input and output, and it stacks with caching.
- **Caching.** The QA and draft calls resend `style.md` and `facts.md` each time; cache reads are 0.1x (0.05x on Opus 5.5).
- **Web fetch** costs nothing beyond tokens. **Web search** costs **$10 per 1,000** plus the result tokens.

**Per-account research at 150 accounts a week (about 650 a month)**

| Design (per account) | Haiku 4.5 | Sonnet 5.5 | Opus 5.5 |
| :- | :- | :- | :- |
| A. Opener from evidence already held: about 2.1k input, 0.2 to 0.4k output | $2.0 a month ($1.0 batch) | $5.3 ($2.7 batch) | $10.7 ($5.3 batch) |
| B. Web-fetch the 2 or 3 pages Clay found: about 12k input, 0.5k output | $9.4 ($4.7 batch) | $18.9 ($9.4 batch) | $37.7 ($18.9 batch) |
| C. 2 searches plus fetch: about 20k input; search fees $13 a month | $27.6 ($20 batch) | $42 | $71 |

The cap is $10 a month, and today's uses (drafts at up to about $0.36 each, QA at about 1 cent a row, about 40 classifications at about 1 cent) take roughly $2 to $4. **Only A fits.** Sonnet with batch is about $2.70; Haiku with batch is about $1. B duplicates Clay's reading. C would need the cap raised to about $30 a month (a paid upgrade: +$20 a month).

## 6. Free and cheap public data

`clients/public.py:67-92` sends GET or HEAD only to `guard.PUBLIC_HOSTS` (`clients/guard.py:60-73`): Greenhouse, Lever, Ashby, Workable, IRS, layoffs.fyi and GitHub (warn-scraper).

| Source | Value | Notes |
| :- | :- | :- |
| **DOL Form 5500 / 5500-SF** (EFAST2 bulk datasets, free) | **Plan-year start**, which gives the renewal window per company. **Schedule A carriers** on life and LTD contracts, which usually bundle a carrier EAP; this maps to the spec's existing `carrier_eap` type. Participant counts check headcount. Welfare feature codes are on line 8b (4A to 4Q) | Welfare plans under 100 participants that are unfunded or insured don't file. So welfare detail covers mostly the 100 to 249 band; the 401(k) 5500-SF covers far more. Match on name plus state or ZIP, like `irs_bmf`. Needs the `dol.gov` and `askebsa.dol.gov` hosts |
| IRS 990 (ProPublica API, free) | Revenue and employee counts for the January nonprofit wave | Already in phase 4 |
| WARN, layoffs | Already planned as a Suppress signal | |
| More SMB ATS boards (BambooHR, SmartRecruiters, Recruitee and similar) | Posting text for 10 to 249 employee firms not on Greenhouse or Lever | Verify each public JSON endpoint before allowlisting |
| Glassdoor or Indeed "burnout" reviews | Tempting, but there's no free API and the terms forbid scraping | Never quote one in copy; don't build |
| Own site | Apollo tracker on /us only; GTM container | Add `/us/industry/*` and `/us/book-demo` as intent paths |

## 7. Top 10 under-used capabilities, ranked by expected effect on interest per lead

**1. Same-day action on /us visits (Apollo visitor filters, plus Instantly subsequences).** A visit after email 1 is the strongest interest the system will see. Today it only reaches the daily post.
- **Sketch:**
  - Add a `website_visitors.search` action to `clients/apollo.py`: company search with `website_visitors_from_domains=["spill.chat"]`, `website_visitors_domain_pages=["/us"]` and `website_visitors_from_past=1`.
  - `sources/site_visits.py` (06:00, plus a midday run) writes facts.
  - For an enrolled account with a pricing or demo visit: post a Slack alert so Harry can add a personal touch, and pull the next step forward by moving the lead into a "US Outbound" subsequence.
  - For an unknown visitor: send it through the front door (SPEC 7).
- **Constraints:**
  - 1 credit a page, or 0 through lookup; watch the 1,200 visitor credits.
  - Copy never mentions the visit.
  - No contact-level tracking.
  - Subsequence copy passes `copy_rules`.
  - Allowed only before any reply (SPEC 1.3).

**2. Named US customer proof from HubSpot.** Proof that fits the industry is the cheapest credibility lever, and SPEC 10 says to "use a named US customer once one exists". HubSpot shows 59 customer companies with country = US.
- **Sketch:** a read-only `sources/hubspot_customers.py` (or `crm/readback.py`) lists customers by industry group and proposes `proof_point` rows; Harry approves the names in the Industries tab.
- **Constraints:** HubSpot reads only; permission to name each customer; check that "country = US" really is a US customer.

**3. A per-account opener from Claude (Batch plus caching, Sonnet or Haiku).** It turns Clay quotes, job-post text and Apollo signals into one specific first line, where today the system fills a template.
- **Sketch:**
  - `enrol/render.py` collects the evidence.
  - A nightly `messages.batches` job in `clients/claude.py` (add `batch_json()`, price at 50%, cache the frozen system prompt) writes `opener_ai` facts.
  - The render-time check and the Monday hand-check gate it.
  - If the line fails, fall back to the template opener.
- **Constraints:** about $1 to $2.70 a month; quotes only from stored facts; copy rules (no "therapy", no statistics); hold back a no-AI-opener control arm for SPEC 12's test.

**4. Dated "why now" triggers and fit tags from free Apollo lookup.**
- **Sketch:** tag sweeps in `sources/apollo_jobs.py` and `apollo_org.py`:
  - People or HR roles posted in the last 30 or 60 days (`organization_job_posted_at_range` with `q_organization_job_titles`);
  - growth over 6 months;
  - Slack or Microsoft Teams users;
  - PEO or HRIS users.

  Add Signals-tab rows ("Uses Slack" with a Slack-native opener; "On a PEO" suggests Upgrade the EAP).
- **Constraints:** lookup's REST path is PHASE0-CONFIRM, otherwise 1 credit a page; Python owns weights; the Control tier stays signal-blind.

**5. Form 5500 renewal timing and carrier-EAP inference.**
- **Sketch:**
  - A monthly `sources/form5500.py` reads the EFAST2 bulk CSVs.
  - Facts: `plan_year_start_month`, `welfare_carriers`, `participants`.
  - Signals: "Renewal in 60 to 120 days" (+20, and the opener "ahead of your {month} plan year"); "Carrier EAP likely" (angle: Upgrade the EAP).
  - Add the DOL hosts to `PUBLIC_HOSTS`.
- **Constraints:** free; partial coverage; never disparage the incumbent EAP (SPEC 10).

**6. Multi-threading with verified-email pre-filtering, funded by lapsing Apollo credits.**
- **Sketch:**
  - `contacts/pick.py` filters people search with `contact_email_status=["verified"]`.
  - Bring SPEC 14's phase-3 second contact forward, and extend it to Standard accounts.
  - `stop_for_company` already stops the whole account on any reply.
- **Constraints:** 1 credit a reveal; same sender and same test version (SPEC 9); about 3,800 to 8,800 credits otherwise lapse.

**7. Polled Apollo waterfall for catch-alls and first-choice roles.** It reaches the People leader rather than a fallback role, and moves spend from scarce Clay credits to Apollo.
- **Sketch:**
  - Allow `run_waterfall_email=true` in `bulk_match` only for misses and catch-alls.
  - Add a poll action for the waterfall result (REST path PHASE0-CONFIRM).
  - Clay Contacts becomes the second fallback.
- **Constraints:** variable credits; keep `email_source` for the 3% bounce kill rule.

**8. Re-engagement on the date the prospect gave (not-now and out-of-office).**
- **Sketch:**
  - `replies/route.py` stores the date.
  - On that date, `poll_approvals` posts a drafted in-thread follow-up for Harry's "send" (`/emails/reply`).
  - `POST /leads/update-interest-status` records the state in Instantly.
- **Constraints:** every post-reply send needs approval (SPEC 1.3); Claude cost is about a cent per draft.

**9. Deliverability settings that are already paid for.**
- **Sketch:**
  - `first_email_text_only=true` in `CAMPAIGN_SETTINGS` (`instantly.py:41`) and the drift check.
  - Provider matching if the registry mixes ESPs.
  - The warmup pool tier checked in `registry/mailboxes.py`.
  - Optional paid upgrade: the Inbox Placement add-on ($47 to $97 a month) through `/inbox-placement-tests`, feeding the seed-inbox kill rule.
- **Constraints:** campaign-level only (SPEC 1.2); never workspace settings.

**10. More hook fields from the same Claygent run.**
- **Sketch:**
  - Extend the "US Outbound – Accounts" output schema (SPEC 8) and `parse_accounts_output` (`clay.py:295`) with `peo_or_broker`, `collab_tool`, `plan_year_month`, `awards` and `hiring_states`.
  - The page reader still only extracts; matching stays in Python.
- **Constraints:** measure credits per account on the first 100 (SPEC 8); track Actions and Data Credits separately.

**Not recommended:**
- Instantly A/Z auto-optimise, AI Reply Agent or Website Visitors.
- HubSpot buyer intent or Breeze prospecting (they break "warm leads only" or the tracking rules).
- Claude web search at scale (over the cap).
- Glassdoor scraping.
- Apollo AI credits: only with an explicit read-only exception from Harry.

## Sources (accessed 1 Oct 2026 unless stated)

- **Repo:** SPEC.md §1, 3, 7, 8, 9, 14; docs/phase0-facts.md; docs/pipeline.md.
- **MCP checks (1 Oct 2026):**
  - Apollo `apollo_users_api_profile`, `apollo_usage_stats_credit_usage_stats` and the tool schemas for `mixed_companies_search`, `organizations_lookup`, `people_bulk_match` and `webhook_result_show`;
  - Clay `get-current-workspace`, `list_subroutines`;
  - HubSpot `get_organization_details`, `search_properties`, `search_crm_objects` (counts) and `search_intent_signals` (refused: needs `buyer_intent.read`);
  - Webflow site head code.
- **Apollo:**
  - API pricing: https://docs.apollo.io/docs/api-pricing
  - Organization search: https://docs.apollo.io/reference/organization-search
  - News articles: https://docs.apollo.io/reference/news-articles-search
  - Job postings: https://docs.apollo.io/reference/organization-jobs-postings
  - Waterfall: https://docs.apollo.io/docs/enrich-phone-and-email-using-data-waterfall
  - Buying intent: https://knowledge.apollo.io/hc/en-us/articles/8047704465933-Buying-Intent-Overview
  - Email status: https://knowledge.apollo.io/hc/en-us/articles/4423314404621-Email-Status-Overview
  - Website visitors: https://knowledge.apollo.io/hc/en-us/articles/20544185285389
  - Release notes 2026: https://knowledge.apollo.io/hc/en-us/articles/43226752968077-Release-Notes-2026
  - Plans: https://phantombuster.com/blog/ai-automation/apollo-pricing/
- **Clay:**
  - Pricing: https://www.clay.com/pricing
  - Actions and data credits: https://university.clay.com/docs/actions-data-credits
  - Pricing 3.0, 11 Mar 2026: https://www.cleanlist.ai/blog/2026-03-12-clay-pricing-changes-2026
  - Signals: https://university.clay.com/docs/signals
  - Job changes: https://www.clay.com/university/guide/signals-job-changes-overview
  - Web intent: https://university.clay.com/docs/website-tracking
  - Public API, 9 Jul 2026: https://www.automationjinn.com/blog/clay-api
  - Plan features: https://www.landbase.com/blog/clay-pricing
- **Instantly:**
  - Inbox placement API: https://developer.instantly.ai/api-reference/groups/inbox-placement-test
  - Interest status: https://developer.instantly.ai/api-reference/lead/update-the-interest-status-of-a-lead
  - Subsequences API: https://developer.instantly.ai/api/v2/campaignsubsequence/listcampaignsubsequence
  - Subsequences help: https://help.instantly.ai/en/articles/7251329-subsequences
  - A/Z testing: https://help.instantly.ai/en/articles/6661549-a-z-testing-how-to-create-email-variants
  - ESP matching: https://help.instantly.ai/en/articles/7044069-email-service-providers-matching
  - AI reply labels: https://help.instantly.ai/en/articles/9712488-ai-custom-reply-labels
  - Campaign schema: https://developer.instantly.ai/api/v2/campaign/createcampaign
  - Pricing: https://lagrowthmachine.com/instantly-pricing/
  - Inbox placement pricing: https://www.amplemarket.com/blog/how-much-does-instantly-really-cost
  - AI Reply Agent: https://instantly.ai/blog/ai-reply-agent-pricing-for-agencies/
  - Website visitors: https://instantly.ai/website-visitors
  - API plan requirements: https://www.salesforge.ai/blog/instantly-api
- **HubSpot:**
  - Buyer intent FAQ: https://knowledge.hubspot.com/data-management/buyer-intent-intent-signals-and-custom-signals-frequently-asked-questions
  - Custom signals: https://knowledge.hubspot.com/data-management/create-custom-signals
  - Breeze credits: https://www.datamagnet.co/post/hubspot-breeze-intelligence-pricing-credits-limits/
  - Prospecting Agent: https://vantagepoint.io/blog/hs/hubspot-breeze-prospecting-agent-credits-pricing-guide
  - Lead scoring: https://www.hubjoy.co/hubspot/lead-scoring
- **Claude:**
  - Pricing (batch, caching, web search and fetch): https://platform.claude.com/docs/en/about-claude/pricing
  - claude-api skill model table, cached 25 Sep 2026.
- **Public data:**
  - Form 5500 instructions: https://www.dol.gov/sites/dolgov/files/EBSA/employers-and-advisers/plan-administration-and-compliance/reporting-and-filing/form-5500/instructions-for-form-5500-annual-report.pdf
  - EBSA data: https://www.dol.gov/agencies/ebsa/researchers/data
  - ProPublica API: https://projects.propublica.org/nonprofits/api
- **Note:** the egress proxy blocked direct fetches of docs.apollo.io, developers.clay.com, developer.instantly.ai and spill.chat. Those facts come from search-result extracts of the cited pages, so the details marked PHASE0-CONFIRM should be checked with one live call each.
