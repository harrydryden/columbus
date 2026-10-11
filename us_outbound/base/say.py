"""How every Slack message reads (Harry, 12 Oct 2026: "Every slack notification should be clear and comprehensible to
anyone, even new users; steer action if action is required, including links or direction; if information only,
pithy").

The shape, built by compose():

    :warning: *This month's queue is lopsided*                    <- headline: what happened, in plain words
    412 companies are queued for October. Only 3% are top-fit.    <- the facts that matter, one short line each
    *What to do:* lower priority_threshold on the <link|General tab> (now 50).   <- only when someone must act
    _Why it matters: ..._                                          <- optional, one line

Rules for the words (kept here so every module writes the same way):
  * No internal names a newcomer cannot know: say "checking for replies", not poll_replies (JOB_NAMES); say "the
    settings sheet", not "the settings in force"; explain a tier or a field in a few words the first time.
  * A message that needs someone says so on its own line, starting "*What to do:*", with a link to the place to do it
    (sheet_link, logs_link) or the exact command to paste (command()).
  * Information only: a headline and at most a few short lines; numbers, not prose.
  * The icon says how urgent it is at a glance (URGENT, ACTION, INFO, DONE).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from us_outbound.context import Context
from us_outbound.logs import log

URGENT = ":rotating_light:"  # something has stopped or is at risk; act now
ACTION = ":warning:"  # needs a look or a decision soon
DECIDE = ":raised_hand:"  # waiting on someone's yes or no
INFO = ":bar_chart:"  # a report; nothing to do
DONE = ":white_check_mark:"  # something finished or recovered

SHEET_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
RAILWAY_LOGS_URL = "https://railway.com/project/{project}/service/{service}?environmentId={environment}"

# What each job does, for a reader who has never seen its name (ops/schedule.py has the schedule).
JOB_NAMES: dict[str, str] = {
    "settings_sync": "reading the settings sheet",
    "settings_bootstrap": "creating the settings sheet",
    "settings_load": "loading the settings sheet",
    "source_universe": "finding new companies",
    "apollo_signals": "collecting buying signals from Apollo",
    "read_pages": "reading company websites",
    "apollo_enrich": "filling in company facts from Apollo",
    "apollo_people": "finding contacts at each company",
    "site_visits": "checking who visited spill.chat",
    "public_signals": "collecting public signals",
    "verify_in_clay": "checking companies in Clay",
    "verify_accounts": "checking company facts",
    "score": "scoring companies",
    "pick_contacts": "choosing who to email",
    "enrol": "adding people to the email campaign",
    "poll_replies": "checking for replies",
    "poll_approvals": "checking the approvals in Slack",
    "hubspot_readback": "updating HubSpot",
    "sync_outcomes": "recording email results",
    "mailbox_health": "checking the sending mailboxes",
    "kill_rules": "checking bounce and complaint limits",
    "blackout": "pausing sends on holidays",
    "daily_post": "the daily summary",
    "monday_readout": "the Monday readout",
    "heartbeat_check": "checking every job ran",
    "suppression_load": "loading the do-not-contact list",
    "lookalikes": "finding companies like our customers",
    "lookalike_leads": "adding lookalike companies",
    "hand_check_post": "the weekly hand-check",
    "retention": "deleting old personal data",
    "label_sample_monthly": "the monthly industry label check",
    "unenrol": "removing people from the campaign",
    "stop": "the emergency stop",
}


def job_name(job: str) -> str:
    """The job in plain words: "checking for replies"; the name itself when there is no description."""
    return JOB_NAMES.get(job, job.replace("_", " "))


def command(cmd: str) -> str:
    """The exact line to paste in a terminal (docs/daily.md: commands run inside the Railway worker)."""
    return f"`railway ssh -- us-outbound {cmd}`"


_TAB_IDS: dict[str, dict[str, int]] = {}  # per sheet, read once a process (each job runs in its own)


def _tab_ids(ctx: Context, sheet_id: str) -> dict[str, int]:
    if sheet_id not in _TAB_IDS:
        _TAB_IDS[sheet_id] = ctx.clients.sheets.tab_ids(sheet_id)
    return _TAB_IDS[sheet_id]


def sheet_link(ctx: Context, tab: str | None = None, label: str | None = None) -> str:
    """A Slack link to the settings sheet, opening on the tab when its id can be read: <url|label>. The tab ids are
    read once a process (a message can link several tabs, and a run can post many messages)."""
    sheet_id = ctx.guard.bounds.settings_sheet_id
    text = label or (f"{tab} tab" if tab else "settings sheet")
    if not sheet_id:
        return text
    url = SHEET_URL.format(sheet_id=sheet_id)
    if tab:
        try:
            gid = _tab_ids(ctx, sheet_id).get(tab)
        except Exception as exc:  # the link still opens the sheet
            log("sheet_tab_id_failed", tab=tab, error=str(exc)[:160])
            gid = None
        if gid is not None:
            url = f"{url}#gid={gid}"
    return f"<{url}|{text}>"


def logs_link(label: str = "the worker's logs in Railway") -> str:
    """A link to the worker's logs, from the variables Railway sets on every service; plain words elsewhere."""
    ids = {k: os.environ.get(f"RAILWAY_{k.upper()}_ID", "") for k in ("project", "service", "environment")}
    if not all(ids.values()):
        return label
    return f"<{RAILWAY_LOGS_URL.format(**ids)}|{label}>"


def compose(icon: str, headline: str, facts: Iterable[str] = (), *, do: str | Iterable[str] = (),
            why: str = "") -> str:
    """One message in the house shape (module docstring). do: the action, or several steps."""
    lines = [f"{icon} *{headline}*", *(f for f in facts if f)]
    steps = [do] if isinstance(do, str) else [s for s in do if s]
    if len(steps) == 1 and steps[0]:
        lines.append(f"*What to do:* {steps[0]}")
    elif steps:
        lines.append("*What to do:*")
        lines += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    if why:
        lines.append(f"_Why it matters: {why}_")
    return "\n".join(lines)


def bullets(items: Iterable[Any]) -> list[str]:
    return [f"• {i}" for i in items]
