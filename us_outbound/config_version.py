"""The config version: what a contact was enrolled under, as one short id (Harry, 7 Oct 2026).

Harry: "ensure that it's easy to continue developing the system whilst live, i.e. that there is a cohort system in
place for contacts that have started not being interrupted by changes. Make this as simple and robust as possible."

Every email a contact gets is rendered when the contact is enrolled and goes to Instantly as the lead's custom
variables, which nothing rewrites afterwards. So a contact's content is fixed at enrolment; what can still change
under them is what every lead in a campaign shares (the step template, the delays, text_only: the campaign
constants, held by registry/mailboxes.IN_FLIGHT_KEYS while leads are in flight). This module names what a contact
was enrolled under, so the cohort report (learn/cohorts.py) can compare like with like and say what changed:

  settings_versions  the effective_from of the version in force of each content tab (CONTENT_TABS): the tabs
                     render and the openers read at enrolment. General and Copy are in by value instead (below),
                     so an edit to live_sending, the Claude cap or a draft Copy row starts no new version.
                     Mailboxes, Tests, States, Focus and Named accounts change who is enrolled and by whom, which
                     each contact records already (sender, copy_version, test_id) and the weekly cuts show;
  copy_hashes        copy_version -> CopyRow.content_hash for every sendable Copy row (approved, QA passed);
  general            the General content keys (CONTENT_KEYS) as text, so a report can say "0.5 → 0.3";
  signature_hash     templates/copy/signature.txt;
  campaign_fingerprint, step_days   the campaign constants: STEP_DAYS, CAMPAIGN_SETTINGS and the step templates
                     (tests/test_cohorts.py pins the fingerprint, so changing one fails a test by name);
  code_sha           the deploy's git commit, RAILWAY_GIT_COMMIT_SHA (Railway sets it on every deploy from GitHub;
                     the image holds no .git), else "dev".

The id is the first 12 hex characters of the sha256 of that snapshot as canonical JSON. enrol computes it once a
run and records it in config_versions (record; dry-run too, SPEC 0.3), and each contact carries it
(contacts.config_version, code_sha, copy_hash; enrol._record_enrolled). log_change writes a config_log row for
each change to what in-flight leads share.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from us_outbound.clients.db import new_id
from us_outbound.clients.instantly import CAMPAIGN_SETTINGS, STEP_DAYS
from us_outbound.context import Context
from us_outbound.logs import log
from us_outbound.settings.model import General, SendWindow, Settings

TABLE, LOG_TABLE = "config_versions", "config_log"
CODE_SHA_ENV = "RAILWAY_GIT_COMMIT_SHA"  # set by Railway on a deploy from GitHub; not a secret
DEV = "dev"  # code_sha outside a Railway deploy
CAMPAIGN_CHANGE, SENDER_NAME = "campaign_change", "sender_name"  # config_log.kind
# config_log.kind too: a campaign paused over the blackout dates, and started again (or left paused) after them
# (registry/blackout.py; Harry, 7 Oct 2026).
BLACKOUT_PAUSE, BLACKOUT_RESUME = "blackout_pause", "blackout_resume"
# The tabs render and the openers read at enrolment (besides General and Copy, which are in by value).
CONTENT_TABS = ("Signals", "Angles", "Industries", "Roles", "Overrides")
# The General keys that shape what a contact gets, or how and when it is sent.
CONTENT_KEYS = (
    "email_format", "email1_subject", "email1_subject_share", "opener_holdout_share", "opener_generic",
    "opener_generic_people", "opener_generic_founder", "opener_generic_ops", "opener_focus", "opener_focus_line",
    "price_from", "booking_link", "booking_page", "site_url", "demo_host", "utm_links", "send_window",
    "second_contact", "second_contact_min_employees", "second_contact_delay_days", "control_share",
    "weekly_enrol_cap",
)
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _hash(value: Any, n: int = 12) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def code_sha() -> str:
    """The deploy's commit, first 12 characters; "dev" outside a Railway deploy (a local run, a test)."""
    return (os.environ.get(CODE_SHA_ENV) or "").strip()[:12] or DEV


def campaign_fingerprint() -> str:
    """12 hex characters over what every lead in a campaign shares: STEP_DAYS, CAMPAIGN_SETTINGS and the step
    templates, HTML and text-only (the unsubscribe line is in them)."""
    from us_outbound.registry.mailboxes import campaign_steps  # it builds on the client, as this module does

    return _hash({"step_days": list(STEP_DAYS), "settings": CAMPAIGN_SETTINGS,
                  "steps_html": campaign_steps(False), "steps_text": campaign_steps(True)})


def signature_hash() -> str:
    from us_outbound.enrol.render import SIGNATURE_TEMPLATE, load_template

    return _hash(load_template(SIGNATURE_TEMPLATE))


def _window(w: SendWindow) -> str:
    days = list(w.days)
    run = days == list(range(days[0], days[-1] + 1)) if days else False
    named = f"{DAYS[days[0]]}–{DAYS[days[-1]]}" if run and len(days) > 1 else ", ".join(DAYS[d] for d in days)
    return f"{named} {w.start:%H:%M}–{w.end:%H:%M} {w.tz}"


def as_text(value: Any) -> str:
    """A General value as the sheet would show it: yes or no, a list joined by commas, a window in words."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, SendWindow):
        return _window(value)
    if isinstance(value, (tuple, list)):
        return ", ".join(as_text(v) for v in value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def general_values(g: General) -> dict[str, str]:
    return {k: as_text(getattr(g, k)) for k in CONTENT_KEYS}


def sendable_hashes(settings: Settings) -> dict[str, str]:
    """copy_version -> content hash, for the Copy rows enrol may send (enrol.sendable_copy's rule)."""
    return {c.copy_version: c.content_hash() for c in settings.copy if c.status == "approved" and c.qa_current}


def _iso(v: Any) -> str:
    return v.isoformat() if isinstance(v, (date, datetime)) else str(v)


@dataclass(frozen=True)
class ConfigVersion:
    id: str
    snapshot: Mapping[str, Any]

    @property
    def code_sha(self) -> str:
        return str(self.snapshot.get("code_sha") or DEV)


def snapshot(settings: Settings) -> dict[str, Any]:
    """Everything a contact enrolled now is rendered and sent under (the module docstring)."""
    return {
        "settings_versions": {t: _iso(settings.versions[t]) for t in CONTENT_TABS if settings.versions.get(t)},
        "copy_hashes": sendable_hashes(settings),
        "general": general_values(settings.general),
        "signature_hash": signature_hash(),
        "campaign_fingerprint": campaign_fingerprint(),
        "step_days": list(STEP_DAYS),
        "code_sha": code_sha(),
    }


def current(ctx: Context) -> ConfigVersion:
    snap = snapshot(ctx.settings)
    return ConfigVersion(_hash(snap), snap)


def record(ctx: Context, cv: ConfigVersion) -> bool:
    """Keep the version's snapshot, the first time it is seen; True when it is new. A database write, so dry-run too."""
    if ctx.store.get(TABLE, config_version=cv.id) is not None:
        return False
    ctx.store.upsert(TABLE, [{"config_version": cv.id, "first_seen": ctx.now, "run_id": ctx.run_id, **cv.snapshot}])
    log("config_version_new", run_id=ctx.run_id, config_version=cv.id, code_sha=cv.code_sha)
    return True


def log_change(ctx: Context, kind: str, campaign: str, keys: Iterable[str], detail: Mapping[str, Any],
               leads_in_flight: int) -> None:
    """One config_log row: a change the leads already in a campaign share (registry/mailboxes.py). Live only, as the
    change itself."""
    if ctx.dry_run:
        return
    row = {"log_id": new_id(), "changed_at": ctx.now, "kind": kind, "campaign": campaign,
           "changed_keys": sorted(keys), "detail": json.loads(json.dumps(dict(detail), default=str)),
           "leads_in_flight": int(leads_in_flight), "code_sha": code_sha(), "changed_by": ctx.job,
           "run_id": ctx.run_id}
    ctx.store.insert(LOG_TABLE, [row])
    log("config_change", run_id=ctx.run_id, kind=kind, campaign=campaign, keys=row["changed_keys"],
        leads_in_flight=row["leads_in_flight"])
