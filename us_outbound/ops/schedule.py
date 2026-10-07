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
timeout:  minutes before the scheduler kills a run. Keep it at 60 or less: ops/heartbeat.py
          treats a "running" row older than 60 minutes as dead.
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
    # weekday (sources/apollo_universe.py); then read_pages, apollo_enrich, apollo_people and verify_accounts, all
    # before pick_contacts at 05:30.
    ScheduledJob("source_universe", "0 3 * * 1-5", live=False, enabled=True, timeout_minutes=60, phase=1),
    ScheduledJob("apollo_signals", "30 3 * * 1-5", live=False, enabled=True, timeout_minutes=45, phase=1),
    # Harry, 2 Oct 2026: our own careers and benefits page reader, in place of Clay's. After apollo_signals and
    # before verify_accounts, whose rescore scores its facts; public GETs and database writes only, so no --live.
    # It stops starting accounts after 30 minutes (sources/pages.py RUN_SECONDS), well inside the timeout.
    ScheduledJob("read_pages", "45 3 * * 1-5", live=False, enabled=True, timeout_minutes=40, phase=1),
    # Harry, 2 Oct 2026: funding and an exact headcount from Apollo's organization enrich, for apollo_enrich_groups.
    # After read_pages starts and before verify_accounts, whose rescore scores its facts before pick_contacts;
    # Apollo reads and database writes only, so no --live. At most 500 accounts a run, inside the timeout.
    ScheduledJob("apollo_enrich", "10 4 * * 1-5", live=False, enabled=True, timeout_minutes=20, phase=1),
    # Harry, 5 Oct 2026: the People leaders at every queue account, from Apollo's free people search, so the People
    # signals score in verify_accounts' rescore, before pick_contacts. It starts no account after 9 minutes
    # (sources/apollo_people.py RUN_SECONDS); Apollo reads and database writes only, so no --live.
    ScheduledJob("apollo_people", "20 4 * * 1-5", live=False, enabled=True, timeout_minutes=20, phase=1),
    # Harry, 5 Oct 2026: the website-visit signals, daily (a visit not read today drops out of Apollo's one-day
    # list). After pick_contacts and before enrol, whose tiers its rescore sets; Apollo reads and database writes
    # only, so no --live (sources/site_visits.py).
    ScheduledJob("site_visits", "0 6 * * *", live=False, enabled=True, timeout_minutes=30, phase=1),
    ScheduledJob("public_signals", "0 4 * * 1", live=False, enabled=False, timeout_minutes=60, phase=1),
    ScheduledJob("verify_in_clay", "30 4 * * 1-5", live=False, enabled=False, timeout_minutes=60, phase=1),
    ScheduledJob("verify_accounts", "30 4 * * 1-5", live=False, enabled=True, timeout_minutes=30, phase=1),
    # score runs inside settings_sync, verify_in_clay, verify_accounts and site_visits (SPEC 9); by hand: `us-outbound rescore`.
    ScheduledJob("score", "", live=True, enabled=True, timeout_minutes=30, phase=1),
    # On for the pilot from Mon 5 Oct 2026, ahead of enrol at 12:00. It writes only to the database
    # (its Apollo calls are reads), so it never needs --live; reveals spend credits within the budget.
    ScheduledJob("pick_contacts", "30 5 * * 1-5", live=False, enabled=True, timeout_minutes=60, phase=2),
    # Enabled for the 5 Oct go-live (Harry, 1 Oct 2026). It runs dry, writing only the database, until
    # live_sending = yes on the General tab: that flag stays Harry's sign-off (SPEC 0.3, 14).
    ScheduledJob("enrol", "0 12 * * 1-5", live=True, enabled=True, timeout_minutes=30, phase=2),
    ScheduledJob("poll_replies", "*/15 * * * *", live=True, enabled=True, timeout_minutes=10, phase=2),
    ScheduledJob("poll_approvals", "*/5 * * * *", live=True, enabled=True, timeout_minutes=4, phase=2),
    ScheduledJob("hubspot_readback", "*/15 * * * *", live=True, enabled=True, timeout_minutes=10, phase=2),
    # Every 15 minutes, not SPEC 9's 01:00: the kill rules and the send forecast need today's sends and
    # stops, and opt-outs are honored the same day (SPEC 13). Offset from poll_replies by 7 minutes.
    ScheduledJob("sync_outcomes", "7-59/15 * * * *", live=True, enabled=True, timeout_minutes=10, phase=2),
    ScheduledJob("mailbox_health", "0 7 * * *", live=True, enabled=True, timeout_minutes=10, phase=0),
    # Brought forward to the first sends (Harry, 1 Oct 2026; docs/gtm-review/README.md §4.2 D4).
    ScheduledJob("kill_rules", "0 * * * *", live=True, enabled=True, timeout_minutes=10, phase=2),
    ScheduledJob("daily_post", "0 9 * * *", live=True, enabled=True, timeout_minutes=10, phase=2),
    # Harry, 6 Oct 2026 ("push ahead with building"): Mondays at 08:30, after the hand-check (08:00) and before the
    # daily post (09:00; SPEC 9 had both at 09:00). It only reads and posts: the alert channel needs --live, and in
    # dry-run the post goes to the dev channel (learn/readout.py).
    ScheduledJob("monday_readout", "30 8 * * 1", live=True, enabled=True, timeout_minutes=20, phase=3),
    # Build additions (README "Deviations").
    ScheduledJob("heartbeat_check", "5 * * * *", live=True, enabled=True, timeout_minutes=5, phase=0),
    ScheduledJob("suppression_load", "30 1 * * *", live=False, enabled=True, timeout_minutes=30, phase=0),
    # The 1st of each month at 02:30, after settings_sync and before source_universe and apollo_signals (Harry,
    # 1 Oct 2026; monthly from 5 Oct 2026: "The customer base for Spill is fairly static"). Day-of-week is *, so
    # the day of the month alone decides (ops/scheduler.Cron.matches). It reads HubSpot and Apollo and writes
    # only the database, so it needs no --live.
    ScheduledJob("lookalikes", "30 2 1 * *", live=False, enabled=True, timeout_minutes=30, phase=1),
    # Harry, 5 Oct 2026: new US accounts like Spill's customers, from Apollo's lookalike search, monthly as the
    # customer base is fairly static. The 1st at 02:50, after lookalikes (02:30), so customer domains are already
    # suppressed, and before source_universe (03:00 on weekdays). HubSpot and Apollo reads and database writes only.
    ScheduledJob("lookalike_leads", "50 2 1 * *", live=False, enabled=True, timeout_minutes=30, phase=1),
    # SPEC 11 weekly hand-check, Monday before that week's enrollment (enrol/hand_check.py).
    ScheduledJob("hand_check_post", "0 8 * * 1", live=True, enabled=True, timeout_minutes=10, phase=1),
    # SPEC 6 and 13: Instantly leads 31 days after their last step (SPEC 9 had it in sync_outcomes), daily at a quiet
    # hour: after Instantly's send window (09:00 to 16:00 US Eastern) and before suppression_load (01:30). It deletes
    # leads in Instantly, so it needs --live, and like every job it stays dry until live_sending = yes
    # (ops/retention.py).
    ScheduledJob("retention", "40 0 * * *", live=True, enabled=True, timeout_minutes=30, phase=2),
)


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
