"""The job schedule (SPEC 9): which jobs the scheduler starts, when, and how.

All times are UK time (SPEC 9): every cron is five fields read in Europe/London local
time, so the clock changes are handled (see ops/scheduler.py). A job with an empty cron
has no schedule of its own; it runs only when started by hand (`us-outbound run <job>`).

live:     the scheduler adds --live. Only jobs that write outside the database (Slack
          #us-outbound, the sheet, HubSpot, Instantly) have it, and a job run with --live
          is still dry until live_sending = yes in the synced settings (SPEC 0.3): a sheet
          edit counts from the next settings_sync (02:00, and 11:30 on weekdays) or
          `us-outbound sync`.
enabled:  False for jobs of a later phase (SPEC 14); the scheduler never starts them.
timeout:  minutes before the scheduler stops a run, and how long a run's "running" row blocks the next
          (ops/health.lock_minutes; W2-4, 20 Oct 2026: it was 60 minutes for every job). At most about 3 times
          the run's observed time (code-platform-ops §2: 1 to 260 s): verify_accounts 15, enrol 15,
          pick_contacts 10, suppression_load 10, mailbox_health 5, and 5 for the light jobs that run hourly or
          daily (kill_rules, blackout, hubspot_readback, daily_post, heartbeat_check, follow_up). A job with a
          time budget of its own keeps a timeout above it (poll_replies 6 minutes, read_pages 30, contact_precheck
          12), and the rest keep theirs until heartbeats.duration_s (persisted from W2-4) shows their p95 at
          production volume.
phase:    the SPEC 14 phase the job belongs to.

This table replaced deploy/jobs.yaml (Cloud Run Jobs and Cloud Scheduler) when the jobs
moved to one Railway worker (Harry, 30 Sep 2026; docs/railway-setup.md).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    cron: str  # minute hour day-of-month month day-of-week, UK time; "" = on demand only
    live: bool
    enabled: bool
    timeout_minutes: int
    phase: int


SCHEDULE: tuple[ScheduledJob, ...] = (
    ScheduledJob("settings_sync", "0 2 * * *", live=True, enabled=True, timeout_minutes=15, phase=0),
    # A second weekday sync (4 Oct 2026), so the morning's sheet edits (Copy approvals, live_sending) are in
    # force for enrol at 12:00. A job may appear twice: the scheduler starts each row on its own cron and never
    # overlaps two runs of one job; by_name() and enabled_names() give each job once (its first row).
    ScheduledJob("settings_sync", "30 11 * * 1-5", live=True, enabled=True, timeout_minutes=15, phase=0),
    # Build, 1 Oct 2026, for the 5 Oct pilot: source_universe and apollo_signals run each weekday, not
    # on the 1st and on Mondays (SPEC 9), to keep the queue two weeks deep with the credits paced by the
    # weekday (sources/apollo_universe.py); then read_pages and verify_accounts, then the paid enrichment of the
    # verified accounts (apollo_enrich, apollo_signals) and the contact pre-check, all before pick_contacts at 05:30.
    ScheduledJob("source_universe", "0 3 * * 1-5", live=False, enabled=True, timeout_minutes=60, phase=1),
    # W1-3a, 19 Oct 2026 (code-sources B4): after verify_accounts and apollo_enrich, for verified accounts only, so no
    # posting is paid for at a company the label check leaves out; it was 03:30, for every new account. No rescore of
    # its own (RS P0-8): pick_contacts' rescore at 05:30 scores its facts.
    ScheduledJob("apollo_signals", "10 5 * * 1-5", live=False, enabled=True, timeout_minutes=45, phase=1),
    # Harry, 2 Oct 2026: our own careers and benefits page reader, in place of Clay's. After apollo_signals and
    # before verify_accounts, whose rescore scores its facts; public GETs and database writes only, so no --live.
    # It stops starting accounts after 30 minutes (sources/pages.py RUN_SECONDS), well inside the timeout.
    ScheduledJob("read_pages", "45 3 * * 1-5", live=False, enabled=True, timeout_minutes=40, phase=1),
    # Harry, 2 Oct 2026: funding and an exact headcount from Apollo's organization enrich, for APOLLO_ENRICH_GROUPS (settings/model.py).
    # W1-3a, 19 Oct 2026 (code-sources B4): after verify_accounts (04:30, its 30-minute timeout), for verified accounts
    # only, the exact count the last size check; it was 04:10, for every new account. No rescore of its own (RS P0-8):
    # pick_contacts' rescore at 05:30 scores its facts. Apollo reads and database writes only, so no --live. At most
    # 500 accounts a run, inside the timeout.
    ScheduledJob("apollo_enrich", "0 5 * * 1-5", live=False, enabled=True, timeout_minutes=20, phase=1),
    # W3-3, 20 Oct 2026 (code-contacts-enrol §7; loop L5): who Apollo has to contact at each verified account waiting
    # for a contact, from its free people search, before pick_contacts (05:30) spends a credit; with the People-leader
    # facts apollo_people wrote at 04:20 for every queue account until then (Harry, 5 Oct 2026), which pick's rescore
    # scores. It starts no account after 12 minutes (contacts/precheck.py RUN_SECONDS); Apollo reads and database writes
    # only, so no --live.
    ScheduledJob("contact_precheck", "15 5 * * 1-5", live=False, enabled=True, timeout_minutes=15, phase=2),
    # Harry, 5 Oct 2026: the website-visit signals, daily (a visit not read today drops out of Apollo's one-day
    # list). After pick_contacts and before enrol, whose tiers its rescore sets; Apollo reads and database writes
    # only, so no --live (sources/site_visits.py).
    ScheduledJob("site_visits", "0 6 * * *", live=False, enabled=True, timeout_minutes=30, phase=1),
    ScheduledJob("verify_accounts", "30 4 * * 1-5", live=False, enabled=True, timeout_minutes=15, phase=1),
    # score runs inside settings_sync, verify_accounts, pick_contacts (at its start, W1-3a) and
    # site_visits (SPEC 9); by hand: `us-outbound run score`.
    ScheduledJob("score", "", live=True, enabled=True, timeout_minutes=30, phase=1),
    # On for the pilot from Mon 5 Oct 2026, ahead of enrol at 12:00. It writes only to the database
    # (its Apollo calls are reads), so it never needs --live; reveals spend credits within the budget.
    ScheduledJob("pick_contacts", "30 5 * * 1-5", live=False, enabled=True, timeout_minutes=10, phase=2),
    # Enabled for the 5 Oct go-live (Harry, 1 Oct 2026). It runs dry, writing only the database, until
    # live_sending = yes on the General tab: that flag stays Harry's sign-off (SPEC 0.3, 14).
    ScheduledJob("enrol", "0 12 * * 1-5", live=True, enabled=True, timeout_minutes=15, phase=2),
    ScheduledJob("poll_replies", "*/15 * * * *", live=True, enabled=True, timeout_minutes=10, phase=2),
    ScheduledJob("poll_approvals", "*/5 * * * *", live=True, enabled=True, timeout_minutes=4, phase=2),
    # Hourly (W2-4, 20 Oct 2026; RS §2 W2-4), not every 15 minutes: a booking needs no answer within the quarter
    # hour, each run reads three days of changes, and its "not ours" records are cached (crm/readback.py).
    ScheduledJob("hubspot_readback", "0 * * * *", live=True, enabled=True, timeout_minutes=5, phase=2),
    # Not SPEC 9's 01:00: the kill rules and the send forecast need today's sends and stops, and opt-outs are honored
    # the same day (SPEC 13). Every 15 minutes inside Instantly's send window (09:00 to 16:00 US Eastern is 13:00 to
    # 22:59 UK on weekdays, both clocks' changes included), hourly otherwise (W2-4, 20 Oct 2026; RS §2 W2-4): 40 + 14
    # = 54 starts on a weekday, 24 on a weekend day, against 96 before. Offset from poll_replies by 7 minutes.
    # heartbeat_check misses it by these rows (health.BY_SCHEDULE): two failed runs in a row, whichever the cadence;
    # health.EXPECTED's 75 minutes is the fallback. Moving the :07 moves when it is missed (the sweep in
    # tests/test_heartbeat.py checks each outage is posted once).
    ScheduledJob("sync_outcomes", "7-59/15 13-22 * * 1-5", live=True, enabled=True, timeout_minutes=10, phase=2),
    ScheduledJob("sync_outcomes", "7 0-12,23 * * 1-5", live=True, enabled=True, timeout_minutes=10, phase=2),
    ScheduledJob("sync_outcomes", "7 * * * 0,6", live=True, enabled=True, timeout_minutes=10, phase=2),
    ScheduledJob("mailbox_health", "0 7 * * *", live=True, enabled=True, timeout_minutes=5, phase=0),
    # Brought forward to the first sends (Harry, 1 Oct 2026; docs/gtm-review/README.md §4.2 D4). Hourly, unchanged.
    ScheduledJob("kill_rules", "0 * * * *", live=True, enabled=True, timeout_minutes=5, phase=2),
    # Harry, 7 Oct 2026 (docs/open-questions.md item 78): pause the campaigns over the blackout dates and start them
    # again after, as Instantly's schedule knows weekdays only (registry/blackout.py). Hourly, deciding in US Eastern
    # dates: the pause lands from 16:00 ET on the last send day before a blackout and the start from midnight ET after
    # it, so many runs fall before 09:00 ET either way, whatever the UK and US clocks do. Instantly writes: --live.
    ScheduledJob("blackout", "40 * * * *", live=True, enabled=True, timeout_minutes=5, phase=2),
    # Tuesday to Saturday from 19 Oct 2026 (the metrics review): each post covers the day before, the previous US send
    # day, so Friday is posted once on Saturday rather than on Saturday, Sunday and Monday (learn/daily_post.py). The
    # Monday readout makes Monday's asks; health.EXPECTED allows the 72 hours from Saturday to Tuesday.
    ScheduledJob("daily_post", "0 9 * * 2-6", live=True, enabled=True, timeout_minutes=5, phase=2),
    # W1-2 (19 Oct 2026; RT §1.6): Mondays at 07:30, before the readout, the reply read checked two ways: Instantly's
    # own counts against our events, and the seed inbox's answer to last week's canary (replies/capture.py). It adds
    # one lead, to the seed inbox only, and deletes it once checked (the canary is sent only once Harry's D10 e is
    # confirmed: capture.SEED_AUTO_REPLY_CONFIRMED): Instantly writes, so --live.
    ScheduledJob("capture", "30 7 * * 1", live=True, enabled=True, timeout_minutes=15, phase=2),
    # Harry, 6 Oct 2026 ("push ahead with building"): Mondays at 08:30, after the hand-check (08:00) and before the
    # daily post (09:00; SPEC 9 had both at 09:00). It only reads and posts: the alert channel needs --live, and in
    # dry-run the post goes to the dev channel (learn/readout.py).
    ScheduledJob("monday_readout", "30 8 * * 1", live=True, enabled=True, timeout_minutes=20, phase=3),
    # W3-1, 20 Oct 2026 (design-final §4): the persisted metrics series, daily at 08:50 UK, after the Monday readout
    # (08:30), which scores last week's forecasts itself, and before the daily post (09:00). It reads the database and
    # writes its metrics table the same in dry-run as live; from W3-4 (21 Oct 2026) it posts the early warning
    # (learn/early_warning.py) to the alert channel once a week when it fires, so --live (still dry until live_sending).
    ScheduledJob("metrics_daily", "50 8 * * *", live=True, enabled=True, timeout_minutes=10, phase=3),
    # Build additions (README "Deviations").
    ScheduledJob("heartbeat_check", "5 * * * *", live=True, enabled=True, timeout_minutes=5, phase=0),
    # HubSpot opt-outs and bounces, and Spill's customers' domains (Harry, 7 Oct 2026: daily, not monthly).
    ScheduledJob("suppression_load", "30 1 * * *", live=False, enabled=True, timeout_minutes=10, phase=0),
    # The 1st of each month at 02:30, after settings_sync and before source_universe and apollo_signals (Harry,
    # 1 Oct 2026; monthly from 5 Oct 2026: "The customer base for Spill is fairly static"). Day-of-week is *, so
    # the day of the month alone decides (ops/scheduler.Cron.matches). It reads HubSpot and Apollo and writes
    # only the database, so it needs no --live.
    ScheduledJob("lookalikes", "30 2 1 * *", live=False, enabled=True, timeout_minutes=30, phase=1),
    # W3-4, 21 Oct 2026 (loop L9; learn/segments.py): the segment prior from Spill 3.0's decided HubSpot deals (one
    # paged read-only search), the 1st at 02:20, after settings_sync (02:00) and before lookalikes (02:30), whose
    # lookalike fit reads it; then the segment value's Suggestions-tab rows, which write the sheet, so --live.
    ScheduledJob("segment_prior_monthly", "20 2 1 * *", live=True, enabled=True, timeout_minutes=10, phase=3),
    # Harry, 5 Oct 2026: new US accounts like Spill's customers, from Apollo's lookalike search, monthly as the
    # customer base is fairly static. The 1st at 02:50, after lookalikes (02:30), so customer domains are already
    # suppressed, and before source_universe (03:00 on weekdays). HubSpot and Apollo reads and database writes only.
    # Parked (W0, 19 Oct 2026; code-sources B6) before its first run on Sun 1 Nov: it pays for whole-US pages of
    # Apollo's search and keeps only the active states. It comes back when it searches the active states alone and
    # accounts(domain) is unique (docs/roadmap.md); `us-outbound run lookalike_leads` still runs it by hand.
    ScheduledJob("lookalike_leads", "50 2 1 * *", live=False, enabled=False, timeout_minutes=30, phase=1),
    # SPEC 11 weekly hand-check (enrol/hand_check.py; W1-5, 19 Oct 2026, in place of hand_check_post at 08:00): the
    # draw, Mondays at 05:45, after pick_contacts (05:30) has found the morning's contacts and before enrol (12:00),
    # from the accounts the coming send days would take. It writes the Hand-check tab and posts the Monday notice, so
    # --live.
    ScheduledJob("hand_check_draw", "45 5 * * 1", live=True, enabled=True, timeout_minutes=10, phase=1),
    # SPEC 6 and 13: Instantly leads 31 days after their last step (SPEC 9 had it in sync_outcomes), daily at a quiet
    # hour: after Instantly's send window (09:00 to 16:00 US Eastern) and before suppression_load (01:30). It deletes
    # leads in Instantly, so it needs --live, and like every job it stays dry until live_sending = yes
    # (ops/retention.py).
    ScheduledJob("retention", "40 0 * * *", live=True, enabled=True, timeout_minutes=30, phase=2),
    # Harry, 11 Oct 2026: the labels judged by hand, a batch a month (ops/label_sample.py). The 1st at 09:15, after
    # the daily post: last month's batch scored, this month's added to the Label sample tab, and both posted with a
    # link to the tab. It writes the sheet and posts to the alert channel, so --live. It asks the models too (the
    # definition drafts and their answer-key passes, learn/definition_suggestions, 10 minutes at most; and from W3-2,
    # 20 Oct 2026, the ICP rule proposals' answer-key passes, learn/precision.propose, 10 minutes more), so its timeout
    # is 30 minutes, above both budgets (the cadence is unchanged).
    ScheduledJob("label_sample_monthly", "15 9 1 * *", live=True, enabled=True, timeout_minutes=30, phase=2),
    # W2-4, 20 Oct 2026 (code-replies-crm M5): each not-now reply whose date has come, one Slack line and a HubSpot
    # task for Harry (replies/follow_up.py). Daily at 08:45 UK, before the daily post; it posts and writes HubSpot,
    # so --live.
    ScheduledJob("follow_up", "45 8 * * *", live=True, enabled=True, timeout_minutes=5, phase=2),
)


# Built jobs taken off the schedule (enabled=False) until their bring-back condition in docs/roadmap.md holds. Unlike
# a job of a later phase they work, and can be run by hand; heartbeat_check does not expect them (W0, 19 Oct 2026).
PARKED: frozenset[str] = frozenset({"lookalike_leads"})


def by_name() -> dict[str, ScheduledJob]:
    """Each job once, by its first row (settings_sync's is 02:00)."""
    out: dict[str, ScheduledJob] = {}
    for j in SCHEDULE:
        out.setdefault(j.name, j)
    return out


def crons(name: str) -> list[str]:
    """Every cron a job runs on, in table order (settings_sync has two)."""
    return [j.cron for j in SCHEDULE if j.name == name and j.enabled and j.cron]


def enabled_names() -> list[str]:
    """The jobs the scheduler starts: enabled and with a schedule (what heartbeat_check expects), each once."""
    return list(dict.fromkeys(j.name for j in SCHEDULE if j.enabled and j.cron))
