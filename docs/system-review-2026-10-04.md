# System review, 4 October 2026

The day before the pilot's first real sends, Harry asked for "an extensive system review and refactor
to ensure that data is flowing as needed, the gates on decisions are in place and coherent, and that it
runs as efficiently as possible whilst being as simple and intuitive to use as possible".

Four reviews read the whole system: data flow, gates, efficiency, and ease of use. Each finding was
graded. A **must** would break or mislead the pilot. A **should** makes it clearer or cheaper without
risk. Every must and should is fixed, tested and deployed. Anything that only matters at higher volume,
or would mean restructuring the day before go-live, is in the plan at the end, with when it is needed.

## What was wrong, and is now fixed

### Data flow

| Found | Fixed |
| :- | :- |
| A campaign left in Draft or Paused in Instantly still got new leads. They sat there unsent, while the account counted as enrolled | enrol reads each campaign's status first. A sender whose campaign is not active gets no new leads, with the reason given (`enrol/capacity.campaigns_not_sending`). The daily post, `golive` and `status` read it the same way |
| A lead Instantly had but did not report as created (a duplicate, or a slow reply) was treated as lost | The campaign is looked up, and the lead id recorded if it is there. A lead Instantly refused is recorded as refused, not proposed again (`enrol.campaign_lead_ids`) |
| Instantly drops bare text outside HTML tags, so every email body would have been blank | Each step's body is wrapped in a `<div>`. Checked against Instantly's read-back on 4 Oct |
| Desk replies were recorded as campaign sends, which inflated the send counts and the kill-rule rates | They are now events of type `reply_sent` |
| `sync_outcomes` re-read every lead each hour | It reads lead statuses hourly with a 6-hour overlap, plus a 2-day re-read once a day, and catches up at most a week per run |
| Sheet edits took effect only at the 02:00 sync, so a morning `live_sending` = yes did not reach the 12:00 enrol | A second sync runs at 11:30 UK on weekdays. `start --live` syncs first, and a `sync` command applies edits straight away |
| "Synced" showed when a setting last changed. After a weekend of good syncs it still said "Friday" | It now shows the last sync that finished ok (`settings/sync.last_read`) |

### Gates

| Found | Fixed |
| :- | :- |
| `optout_tested` = no did not stop a live send | enrol's gate stops live sends until it is yes. So does the re-check at each ✅ |
| A ✅ that could not go through yet (sending stopped, a reply waiting too long) closed the card, and the approval was lost | The re-check tells **holds** (temporary: the card stays open and the ✅ stands) from **blocks** (permanent: the card closes and nothing is sent) |
| HubSpot was checked when the card was posted, but not when it was approved, up to a day later | HubSpot is asked again at the ✅. A company that is now a customer or has an open deal is blocked and excluded |
| enrol and the send approvals each had their own version of "can this company be emailed" | One check, `enrol.eligible`, used by both |
| The daily post and `golive` checked gates that enrol no longer uses, and missed some it does use | Both now call enrol's own gate. The hand-check shows only while `auto_send` = yes |
| The bot's own ✅ ❌ reactions could be read as a decision | The bot's reactions are skipped. Only approvers count |
| `--set` could change a sign-off key from the command line | `settings load --set` refuses `live_sending`, `auto_send`, `optout_tested` and `approver_slack_ids`. These are set by hand on the sheet |
| `stop` and `unenrol` could fail to run if the sheet was unusable | They run on the General defaults, so the brake always works |
| After go-live, a newly created campaign stayed in Draft until someone ran `start` | The 07:00 mailbox check activates it. It holds only approved leads, so this sends nothing by itself |

### Efficiency

| Found | Fixed |
| :- | :- |
| `poll_approvals` read each Slack thread twice (messages, then reactions) | One read per item: reactions come from the thread just read |
| Hot reads scanned whole tables | Indexes on accounts(status), events(type, occurred_at), signal_events(source, fact) and hitl_items(kind, status), and a `Store.latest` query for "last run" lookups |
| The daily post loaded every account and contact | It filters in SQL |
| A long `poll_replies` run could overlap the next one | It stops starting new replies after 6 minutes. The next run takes the rest |
| Campaign drift (the daily limit or the sending list) needed a manual `campaigns ensure --fix` | The morning mailbox check puts these two right itself. Other drift is reported |
| Jobs ran at most two at a time, so jobs due together waited for each other | Three at a time (`US_OUTBOUND_MAX_PARALLEL` = 3 on Railway) |

### Ease of use

| Found | Fixed |
| :- | :- |
| No single page said what Harry does each day | `docs/daily.md`: the two switches, the brake, the day in #us-outbound, the Slack words and ten commands |
| `status` opened with job tables | It opens with the switches, when the settings were synced and what waits for you. Then it lists only the jobs that need a look |
| The daily post buried what needed Harry | The "Needs you" line sits under the headline |
| Command words differed from the Slack words | `approvals send / contact / company` and `replies send` match the Slack words. Build-time commands are hidden from `--help` |
| A `--live` run that stayed dry did not say why | It says that `live_sending` is no in the synced settings, when they were synced, and to run `us-outbound sync` |
| Setting `live_sending` = no looked like a brake, but Instantly keeps sending follow-ups | `golive`, the sheet's note and the daily guide all say the brake is `stop --live` |
| The sheet's notes were out of date | General notes rewritten (switches, approvers, opt-out, HubSpot ids, links). Unused keys say "Not used yet; leave as it is" |
| The mailbox check posted every morning | It posts only news: a promotion, a limit raised, a campaign created or activated, or a problem |

## How the gates fit together now

A lead reaches Instantly only if every row holds, top to bottom.

| Gate | Where | When it fails |
| :- | :- | :- |
| `--live` and `live_sending` = yes | `bootstrap.resolve_live`, every job | The job runs dry. enrol posts a few preview cards to #us-outbound-dev |
| A send day, not a blackout date | `enrol.gate` | No cards today |
| `optout_tested` = yes | `enrol.gate` (live runs) | No cards. golive FAILs |
| No operator stop, stop rule or positive reply waiting over 24 hours | `enrol.gate` | No cards. The daily post says which |
| This week's hand-check approved, only with `auto_send` = yes | `enrol.hand_check` | No cards |
| The company and contact can be emailed: tier, suppression, partners, HubSpot exclusions, industry on, not pulled | `enrol.eligible` | That company is skipped |
| The sender's campaign is active in Instantly | `capacity.campaigns_not_sending` | That sender gets no new leads |
| The sender has room: the ramp, a quarter of the daily cap per day, the weekly cap | `limits.today` | Fewer cards |
| An approver's ✅, with `auto_send` = no | `enrol/approvals.py` | The card lapses after the next send day. The company goes back to the queue |
| The re-check at the ✅ | `approvals.recheck` | A hold keeps the ✅ until it clears. A block closes the card |

The brake is `us-outbound stop --live`. It pauses every campaign and stops enrollment.

## Deferred, with when each is needed

None of these affects the pilot at its first weeks' volume (about 6 to 11 cards a day). The dates are
proposals for Harry to confirm.

**Efficiency, as volume grows**

| Item | Needed by |
| :- | :- |
| `sync_outcomes`: delete Instantly leads 31 days after their last step, which caps the lead list. Ask only for bounced or unsubscribed leads, if `/leads/list` filters by status | Before about 120 sends a day (weeks 3 to 4) |
| `poll_approvals`: read a thread only when something changed (one channel read per run) | Before about 120 open cards |
| Rescore: skip enrolled and engaged accounts, load only the latest fact per source, write only changes | Before about 10,000 accounts |
| `hubspot_readback`: one deals search by pipeline and last-modified date, instead of one per warm account | Within a month |
| `kill_rules`: window its reads of send, bounce and reply events | Within two months |
| Claude spend: classify out-of-office replies without the model (Instantly's auto-reply flag), and give copy QA a sub-cap. Raising the $10 cap with the ramp is Harry's call | Before about 80 new accounts a day |
| Heartbeats kept 90 days. `suppression_load` incremental by last-modified. `verify_accounts` paces HubSpot searches | Within two months |

**Gates and data**

| Item | Needed by |
| :- | :- |
| Blackout dates: pause the campaigns over a blackout, since Instantly's schedule has none. (A ✅ whose email 1 would land on one is already held) | Before Thanksgiving, 26 November |
| HubSpot "active sequence" and "another user's activity in 90 days" exclusions are read from the sheet but never checked. Add both checks | Week 2 |
| The "CA/WA share of US staff" and "fewer than 5 US people" exclusions have no input (no people-location data). Compute them from a people search, or document them as not checked | Week 3 |
| Kill rules with no input: spam complaint, seed spam and block bounce. Add a `record` command for seed results, and keep the bounce reason | Week 2 |
| The guard has no approval rule for adding a lead. It relies on the approval flow. Mirror the reply rule while `auto_send` = no | Week 2 |
| `unenrol` deletes leads but leaves accounts enrolled, so capacity keeps their slots | Week 2 |
| The send forecast assumes email 1 goes the day it is approved. A ✅ after 16:00 ET sends the next day | When volume makes it matter |

**Simplicity**

| Item |
| :- |
| One shared Slack helper module: the reply desk and the send approvals each have their own copies of about eight helpers |
| One timestamp parser (20 copies in 11 variants) |
| Retire the `reply_approval` kind name once production has none (check with a query first) |
| Remove the scoring-time angle opener (`scoring/angle.py`), which is never used in email, and relax the Angles tab's `default_opener` requirement |
| Wire in the domain-redirect follower (`clean/domains.follow_redirect`) or delete it. It is built but never called |
| Operator commands print raw JSON. Print one sentence first, and the JSON only with `--json` |

## Before the first send

Harry's steps, in order. `us-outbound golive` shows what is still open.

1. **Opt-out test.** Send the unsubscribe link to a seed inbox and check it works. Then set
   `optout_tested` = yes on the General tab.
2. **Copy.** Approve the copy rows still waiting on the Copy tab.
3. **Switch on.** Set `live_sending` = yes. Then run `us-outbound start --live`, which syncs the sheet first.
4. **First card.** The first ✅ should be a seed-inbox lead. Use it to check that the email body
   renders and the unsubscribe link works before approving prospects.

If anything looks wrong: `us-outbound stop --live`.
