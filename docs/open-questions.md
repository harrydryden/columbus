# Open questions for Harry

SPEC says: "If something is missing or ambiguous, stop and ask Harry rather than guess." Every item below either blocks progress or has a safe default already in the code, so the build is not stalled on any of them. To change a default, edit the sheet where the item lives there, or say which item and the code changes.

## A. Needed to finish Phase 0 (blocking)

1. **Privacy and opt-out page.** `/us/legals/privacy-notice` is still a Webflow draft, and there is no US opt-out page. Every email links to it (SPEC 10), and render blocks any send while `privacy_url` is blank. What is the live URL?
   *Deferred (Harry, 30 Sep): on the website; not needed yet. Render still blocks sends while `privacy_url` is blank, so this is needed before phase 2 goes live.*
2. **Postal address.** Please give Spill's UK registered address as it should appear in the footer (`postal_address`).
   *Deferred (Harry, 30 Sep): not needed yet; needed before phase 2 goes live.*
3. **Footer and Article 14 text.** Please approve or edit `templates/copy/footer.txt` and `templates/copy/article14.txt` (SPEC 14: "For Harry to approve"). The notice names Apollo and Clay as sources for every contact, and gives the lawful basis as legitimate interests.
   *Deferred (Harry, 30 Sep): not needed yet; needed before phase 2 goes live.*
4. **Instantly.** We have no connector or key in this session. Please add the API key to Secret Manager. The plan facts in SPEC 14 are still unconfirmed: plan tier, email and contact caps and current use, and whether the emails, reply, forward and accounts endpoints exist.
5. **Google Cloud.** Which project and region should hold Cloud Run, Scheduler, Secret Manager and Artifact Registry? BigQuery `us_outbound` will go in EU, next to the existing datasets.
   *Answered (Harry, 30 Sep): a separate project, `columbus`. Region defaults to europe-west2 (London); BigQuery in EU.*
6. **Small Google Cloud costs (SPEC 1.1: no new paid services).**
   - Cloud Scheduler is $0.10 per job per month beyond 3 free jobs. There are about 15 jobs by phase 3, so about $1.20 a month.
   - Secret Manager is about $0.06 per secret per month beyond 6 free. We have 7 secrets, so about $0.06 a month.
   - Artifact Registry is free up to 0.5 GB.

   Do you approve these costs?
7. **Clay.**
   - What are the plan tier and the monthly credit pool? `clay_monthly_credits` stays 0 until you tell me, so no Clay calls happen.
   - Is the Routines (function) API enabled for the workspace, with "API & CLI" ticked on the two functions? If not, we use the CSV fallback in SPEC 8.
   - The "US Outbound" folder and its two functions are built in Clay's UI.
8. **Settings sheet.** A sheet the service account creates is visible only to that account. Shall I create "US Outbound – Settings" in your Drive from the defaults, for you to share with the service account? Or would you rather create it yourself?
9. **Slack.** Please create the app from `deploy/slack-app-manifest.yaml`, then create #us-outbound and #us-outbound-dev and invite the bot. Neither channel exists yet.
10. **Apollo tracker.** In Apollo:
    - fix "us/pricing" → "/us/pricing"
    - add "/us/book-demo" as high intent
    - confirm data starts arriving (`data_received` is false)
    - confirm the script is not on employee-facing pages
11. **HubSpot private-app token.** Create it with the SPEC 13 scopes before 26 Oct.
12. **Clay in dry-run.** Should Clay functions run while `live_sending = no`? Phase 1 verifies the queue before any send, so they need to. The default is yes: Clay runs within the monthly budget in dry-run too, because it sends nothing to prospects.

## B. Decisions with a default in place

The default is in brackets. Items marked PHASE0-CONFIRM are checked against the real system during phase 0 setup.

### Settings sheet and signals

13. First People hire uses `people_leader_count`, which comes from apollo_people. The row lists "apollo_jobs, apollo_people". [both]
14. Terms match whole words, so "mental health day" does not match "mental health days". Should plurals be added to Progressive benefits? [SPEC terms as written]
15. Should the Nonprofits rows exclude NAICS 813110 (religious organizations), since churches are off? [not excluded]
16. Each of the 9 "Off" industry groups is one inactive row named after the group, until you add its website labels. [as described]
17. Please check the Apollo keyword chosen for each Tech label (e.g. software, agtech, video games). [as drafted]
18. Industries `landing_page_url` and `proof_point` are blank ("Harry to fill"). A blank proof point blocks step 2 for that group. [blank]
19. No default signal has an opener, so every account gets its angle's default opener. Should "EAP named" ship with "Saw your benefits page mentions {evidence}."? [blank]
20. Validation adds checks beyond SPEC. Are they OK? [all on]
    - daily_cap is at most 30
    - claude_monthly_cap_usd is at most 10
    - live needs approver ids
    - an empty tab is rejected, except Overrides and Tests
    - CA or WA set active is rejected
    - a running test needs approved copy
    - dev_channel must differ from alert_channel
21. Dates are typed as YYYY-MM-DD and shares as 0.15, not as formatted percentages. [yes]
22. Both copy versions share subjects, so the first A/B test isolates the opener. [yes]
23. What goes in the Copy tab's "sources" column? [blank]
24. `claude_model` is `claude-haiku-4-5` (SPEC: "a current fast Claude model"). Classification, drafts and readouts cost well under the $10 cap even on a larger model. [Haiku 4.5]

### Scoring and tiers

25. A negative total score is not floored at 0. [not floored]
26. While the latest Clay careers read is blocked or error, that source gives no points, even from an earlier good read that is still fresh. Hold, Exclude and Suppress signals still count. [as described]
27. When several signals suggest the chosen angle, the one with the largest weight sets the opener. [yes]
28. An unknown or inactive HQ state makes the account Excluded rather than Held. [Excluded]
29. The partner keyword "payroll" and NAICS 524 over-exclude a little: CPA firms that list payroll, and insurtechs. [keep, the safe direction]
30. Founded less than 2 years ago uses 1 January when only the year is known. [yes]
31. The tier-mix alert (5–40%) posts once per rescore, with its bounds in code. Should it post once a day, with the bounds on the General tab? [per rescore, in code]
32. While FL is off, the FL staff share is added to the CA/WA share before the 20% test. This is the stricter reading of SPEC 9. [added]
33. Signal matches and tiers are recomputed on every rescore, including for enrolled accounts. Should they freeze at enrollment, so the Monday signal table measures what was true at send time? [recomputed]

### Contacts and cleaning

34. Only EMEA and APAC titles are skipped, not APJ, LATAM, Europe, UK or Asia. [only EMEA and APAC]
35. RevOps counts as sales ops and is skipped. A title is skipped if a skip word appears anywhere in it. [yes]
36. The Roles tab has no CHRO, VP HR, Head of HR, Director of People, Owner or Principal, so those titles are never contacted. Should they be added to the sheet? [not added]
37. Company-name casing is kept as given, so "acme creative" stays lower case. [kept]

### Copy and rendering

38. The ask sentences are constants in render.py. [as below]
    - People leader: "Would a 20-minute walkthrough be useful?"
    - Founder or executive: "Worth a look for the team?"
    - Operations: "Happy to send the one-pager if that's useful."
39. Step 4 offers the one-pager on reply, because there is no link variable. Should there be a `{{one_pager}}` variable? [no variable]
40. Must every email mention same-day counseling? [not enforced]
41. "therapy" and "therapist" are blocked even inside a quote from the prospect's page, and the opener then falls back to the default. [blocked]
42. Please review the phrase lists for disparaging an EAP and for EAP in Spill's name, in `enrol/copy_rules.py`. [short lists]
43. Statistics: any percentage except the 30% utilization claim is blocked, as are "3x", "2 in 3" and "500+ companies". Prices and durations are allowed. [yes]
44. "wellbeing" is blocked as British; use "well-being". [blocked]
45. The signature ends "spill.chat/us", which mail clients turn into a link, so it can't go in step 1. The footer already names the sender. [yes]

### Enrollment and sending

46. The running test takes version_a's angle accounts. Control fills any shortfall in Priority and Standard, and the other way round. [yes]
47. Weekly hand-check: enrollment waits until this week's hand-check item is marked handled, and pulled accounts are skipped. How do you want to approve it: a Slack thread reply, or the sheet? [a handled item in BigQuery; the Slack approval flow comes with phase 2]
48. Your two addresses share one campaign. Leads use your first Active mailbox's signature. [yes]
49. While `hubspot_owner_id` is blank, the HubSpot re-check excludes any account that has an owner. [fails closed]
50. `mailbox add` joins the campaign only once the mailbox is Active. SPEC's campaign table sends from Active addresses only. [Active only]
51. A mailbox is warm at a warmup or health score of 90 or more, or after 21 days of warmup. [90, in code] (PHASE0-CONFIRM)
52. `unenrol --month` removes that month's Instantly leads but leaves BigQuery status unchanged. [unchanged]
53. `stop` and `start` are recorded as heartbeat rows. `start` refuses while any campaign has drifted. [yes]
54. Operator commands that never reach a prospect (stop, mailbox, erase, setup) are live with `--live` alone. Jobs and `start` also need `live_sending = yes`. [as described]
55. Lead imports skip anyone already in any Instantly campaign, the EU ones included, and spend no Instantly verification credits. [yes]
56. The Instantly blocklist gets email addresses only, because a domain entry would also block EU campaigns. Domain suppression stays in BigQuery. [emails only]

### Instantly, Apollo and HubSpot details (PHASE0-CONFIRM)

57. Instantly has no America/New_York in its time-zone list, so campaigns use America/Detroit (same rules). Check that a created campaign shows Mon–Fri and steps on days 0, 3, 8 and 15.
58. Instantly: confirm the custom-variable length limit (worst case for s1_body is about 1,500 characters), that newlines survive in text-only mode, that the forward endpoint and is_evergreen behave as expected, and that step 2–4 sends stay on the step-1 address.
59. Apollo: `apollo_floor` and the monthly budget count lead credits. Visitor discovery uses organization search with website-visitor filters (1 credit to confirm). bulk_match never runs the email waterfall; misses go to Clay.
60. HubSpot: confirm the deal→company and contact→company association ids and the v4 unsubscribe-all endpoint. Suppression loads hard bounces only. Company match is on the primary domain and its www. form.

### Data and operations

61. `contacts.enrolment_month` is STRING "YYYY-MM". v_signal_value flags a signal after 200 accounts with step 1 delivered. Site-visit events are logged for enrolled accounts only, as SPEC says. [yes]
62. Raw tables (including raw_clay_contacts, which holds names and emails) have no retention rule, but erase covers them. Should they follow the 12-month contacts rule? [kept]
63. `erase` GDPR-deletes the HubSpot contact whoever created it, lists a manual Clay step, and deletes BigQuery rows even in dry-run. [yes]
64. heartbeat_check alerts once when a job is newly missed and repeats at 09:00 UK. `test read` before the read date is labelled an early look. [yes]
