"""Cohorts (config_version.py; Harry, 7 Oct 2026): what a contact was enrolled under, and the campaign constants
every lead in flight shares.

Harry: "ensure that it's easy to continue developing the system whilst live, i.e. that there is a cohort system in
place for contacts that have started not being interrupted by changes."
"""

from __future__ import annotations

import dataclasses
import inspect
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.fakes import FakeTransport, make_context
from tests.test_registry import SETTINGS
from tests.test_render import COPY, copy_row, make_settings
from us_outbound import config_version
from us_outbound.clients import instantly as instantly_client

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)  # Monday 9 Nov 2026, 12:00 UK


# -- the campaign constants -----------------------------------------------------------------------------------------


def test_campaign_constants_are_pinned():
    """If this fails you changed what every in-flight lead receives or when (STEP_DAYS, the step template, the campaign
    settings). Follow docs/developing-while-live.md: bump this literal, deploy, then `campaigns ensure --fix
    --in-flight --live` or wait for the campaigns to drain (`us-outbound cohorts in-flight`)."""
    assert config_version.campaign_fingerprint() == "9dd3b701a130"


def test_a_lead_is_never_patched_with_variables():
    """A lead's emails are its custom variables, set once by POST /leads/add: the only lead PATCH there is sets its
    status (set_lead_paused), and nothing else in the package sends a lead's variables anywhere."""
    src = inspect.getsource(instantly_client.Instantly)
    patches = re.findall(r'"PATCH", f"/leads/[^\n]*\n[^\n]*\n\s*json=([^,]+),', src)
    assert patches == ['{"status": status}']
    root = Path(instantly_client.__file__).resolve().parents[1]
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r'"PATCH",\s*f?"/leads', text) or path.name == "instantly.py", path
    t = FakeTransport()
    t.route("GET", "/campaigns", {"items": [{"id": "c1", "name": "US Outbound – Hannah Spalding"}]})
    t.route("GET", "/leads/L1", {"id": "L1", "campaign": "c1"})
    ctx = make_context(SETTINGS, live=True, transport=t)
    ctx.clients.instantly.set_lead_paused("US Outbound – Hannah Spalding", "L1", True)
    [patch] = [r for r in t.requests if r.method == "PATCH"]
    assert patch.json == {"status": instantly_client.LEAD_PAUSED}


# -- the config version -----------------------------------------------------------------------------------------------


def versioned(settings, **tabs):
    return dataclasses.replace(settings, versions={**settings.versions, **tabs})


def test_config_version_changes_only_for_content_tabs(monkeypatch):
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    t0 = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)
    base = versioned(make_settings(), Signals=t0, General=t0, **{"Named accounts": t0, "Mailboxes": t0})
    ctx = make_context(base)
    first = config_version.current(ctx)
    later = t0 + timedelta(days=1)

    def vid(settings):
        ctx.settings = settings
        return config_version.current(ctx).id

    # Who is enrolled, by whom, and the General keys that shape nothing sent: the same version.
    assert vid(versioned(base, **{"Named accounts": later, "Mailboxes": later, "General": later})) == first.id
    assert vid(dataclasses.replace(base, general=dataclasses.replace(base.general, live_sending=True))) == first.id
    assert vid(dataclasses.replace(base, copy=(*COPY, copy_row("legal-v1", "Legal Teams", status="draft")))) == first.id
    # What a contact gets: a new version.
    assert vid(versioned(base, Signals=later)) != first.id
    assert vid(dataclasses.replace(base, general=dataclasses.replace(base.general, price_from=250))) != first.id
    reworded = copy_row("agencies-v1", COPY[0].industry, bodies={**{n: COPY[0].step(n).body for n in (1, 2, 3, 4)},
                                                                 2: "A new email 2 for {{first_name}}."})
    assert vid(dataclasses.replace(base, copy=(reworded, COPY[1]))) != first.id
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a4c5c27f00ba7e1234567890")
    deployed = config_version.current(make_context(base))
    assert deployed.id != first.id and deployed.code_sha == "a4c5c27f00ba"
    assert first.code_sha == "dev"
    assert set(first.snapshot) == {"settings_versions", "copy_hashes", "general", "signature_hash",
                                   "campaign_fingerprint", "step_days", "code_sha"}
    assert first.snapshot["settings_versions"] == {"Signals": t0.isoformat()}
    assert first.snapshot["general"]["send_window"] == "Mon–Fri 09:00–16:00 America/New_York"


def test_a_version_is_recorded_once_and_dry_run_records_it_too():
    ctx = make_context(make_settings(), now=NOW)
    cv = config_version.current(ctx)
    assert ctx.dry_run and config_version.record(ctx, cv) is True
    ctx.now = NOW + timedelta(days=1)
    assert config_version.record(ctx, cv) is False
    [row] = ctx.store.tables["config_versions"]
    assert row["config_version"] == cv.id and row["first_seen"] == NOW and row["copy_hashes"] == {
        c.copy_version: c.content_hash() for c in COPY}


def test_a_change_is_logged_only_when_live():
    ctx = make_context(SETTINGS, now=NOW, job="campaigns_ensure")
    config_version.log_change(ctx, config_version.CAMPAIGN_CHANGE, "US Outbound – Sam Jackson", ["steps.1"], {}, 3)
    assert ctx.store.tables["config_log"] == []
    ctx.guard.configure(live=True)
    config_version.log_change(ctx, config_version.CAMPAIGN_CHANGE, "US Outbound – Sam Jackson", ["steps.1"],
                              {"steps.1": [{"body": "new"}, {"body": "old"}]}, 3)
    [row] = ctx.store.tables["config_log"]
    assert (row["kind"], row["campaign"], row["changed_keys"], row["leads_in_flight"], row["changed_by"]) == (
        "campaign_change", "US Outbound – Sam Jackson", ["steps.1"], 3, "campaigns_ensure")
