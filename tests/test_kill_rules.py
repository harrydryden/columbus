"""kill_rules (SPEC 12 "Kill rules"; learn/kill_rules.py, learn/holds.py): the rules, the registry pause
path, suppression, the holds enrol and the registry honour, the alert, and the killrules command."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from tests.test_registry import (
    C_HANNAH,
    C_HARRY,
    C_SAM,
    HANNAH,
    HARRY,
    HARRY2,
    NOW,
    SAM,
    SETTINGS,
    row,
    setup,
)
from us_outbound.context import Secrets
from us_outbound.enrol import capacity, enrol
from us_outbound.learn import holds, kill_rules
from us_outbound.logs import hash_email
from us_outbound.registry import mailboxes as reg

RECENT = NOW - timedelta(days=2)


def campaigns(inst):
    inst.standard(C_HANNAH, [HANNAH], 30)
    inst.standard(C_SAM, [SAM], 30)
    inst.standard(C_HARRY, [HARRY, HARRY2], 60)


def sends(ctx, mailbox: str, n: int, *, bounced: int = 0, at=RECENT, source: str = "apollo", block: str = "",
          group_account: dict | None = None, prefix: str = "") -> list[str]:
    """n step-1 sends from mailbox, each to its own contact and account; the first `bounced` bounce."""
    prefix = prefix or f"{mailbox.split('@')[0]}-{mailbox.split('@')[1].split('.')[0]}-{at:%m%d}"
    events, contacts, accounts = [], [], []
    for i in range(n):
        cid, aid = f"c-{prefix}-{i}", f"a-{prefix}-{i}"
        t = at + timedelta(minutes=i)
        events.append({"event_id": f"s-{cid}", "type": "sent", "mailbox": mailbox, "contact_id": cid,
                       "account_id": aid, "step": 1, "occurred_at": t})
        contacts.append({"contact_id": cid, "account_id": aid, "email": f"p{i}@{prefix}.com", "email_source": source})
        accounts.append({"account_id": aid, "domain": f"{prefix}{i}.com", "status": "enrolled", **(group_account or {})})
        if i < bounced:
            events.append({"event_id": f"b-{cid}", "type": "bounced", "contact_id": cid, "step": 1,
                           "occurred_at": t + timedelta(minutes=5), "reply_text": block or "550 5.1.1 user unknown"})
    ctx.store.insert("events", events)
    ctx.store.insert("contacts", contacts)
    ctx.store.insert("accounts", accounts)
    return [c["contact_id"] for c in contacts]


def items(ctx, status=None):
    rows = ctx.store.select("hitl_items", {"kind": "kill_rule"})
    return [r for r in rows if status is None or r["status"] == status]


def slack_posts(t):
    return [r for r in t.requests if r.url.endswith("chat.postMessage")]


def later(ctx, t, days: int):
    """The same store and Instantly, `days` on."""
    from tests.fakes import make_context

    nxt = make_context(ctx.settings, live=ctx.live, transport=t, now=ctx.now + timedelta(days=days), store=ctx.store)
    nxt.clients.sheets = ctx.clients.sheets
    return nxt


# -- a mailbox's bounce rate ---------------------------------------------------------------------------


def test_a_mailbox_over_3_percent_is_paused_through_the_registry_path():
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    bounced = sends(ctx, HANNAH, 40, bounced=2)[:2]
    out = kill_rules.run(ctx)
    [fired] = out["fired"]
    assert fired["rule"] == "mailbox_bounce_rate" and fired["target"] == HANNAH and fired["action"] == "pause_mailbox"
    assert "2 of its last 40 sends bounced (5.0%; the limit is 3%)" in fired["reason"]
    # The registry pause path: Paused on the Mailboxes tab, and off its campaign's sending list.
    assert row(sheets, HANNAH)["status"] == "Paused"
    assert inst.by_name(C_HANNAH)["status"] == 2
    assert out["paused"][HANNAH].startswith("Paused on the Mailboxes tab; US Outbound – Hannah Spalding: paused")
    # The hold is in force at once, whatever the synced settings still say.
    [item] = items(ctx, "open")
    assert item["payload"]["mailboxes"] == [HANNAH] and item["payload"]["dry_run"] is False
    assert item["payload"]["campaigns_paused"] == [C_HANNAH]  # mailbox_health starts it again once she is back
    assert holds.held_mailboxes(ctx.store) == {HANNAH: fired["reason"]}
    assert [m.status for m in holds.with_holds(ctx.store, SETTINGS).mailboxes if m.address == HANNAH] == ["Paused"]
    assert "Hannah Spalding" not in capacity.sending_capacity(ctx.store, SETTINGS, ctx.now_et().date())
    # Alerted in the alert channel, mentioning the approver, with the item to clear.
    [post] = slack_posts(t)
    assert post.json["channel"] == "C_ALERT"
    assert post.json["text"] == (
        "<@U_HARRY> 🛑 Safety stop, Tue 27 Oct 12:00 UK\n"
        "• hannah@meetspill.org paused: 2 of its last 40 sends bounced (5.0%; the limit is 3%). Hannah's approved "
        "leads wait; nothing else changes. To resume once fixed: set it Active on the Mailboxes tab, then "
        f"`railway ssh -- us-outbound killrules clear {fired['item_id']} --live`.")
    assert item["slack_ts"] == "1.1"
    # Bounced contacts are suppressed.
    assert out["suppressed"] == 2
    hashes = {r["email_sha256"] for r in ctx.store.select("suppression")}
    assert {hash_email(f"p{i}@hannah-meetspill-{RECENT:%m%d}.com") for i in range(2)} <= hashes
    assert all(ctx.store.get("contacts", contact_id=c)["suppressed"] for c in bounced)


def test_the_rule_does_not_fire_twice_and_old_bounces_never_count_again():
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    sends(ctx, HANNAH, 40, bounced=2)
    kill_rules.run(ctx)
    again = later(ctx, t, 0)
    assert kill_rules.run(again)["fired"] == [] and len(slack_posts(t)) == 1
    # Harry checks it, sets it Active again and clears the hold: the old bounces stay counted.
    [item] = items(ctx, "open")
    kill_rules.clear(again, item["item_id"], "harry")
    assert holds.held_mailboxes(ctx.store) == {}
    assert kill_rules.run(later(ctx, t, 1))["fired"] == []


def test_replies_sent_from_the_desk_are_not_sends_in_a_bounce_rate():
    """2 bounces in 50 campaign sends is 4%; counting 30 desk replies as sends would make it 2.5% and hide it."""
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    sends(ctx, HANNAH, 50, bounced=2)
    ctx.store.insert("events", [{"event_id": f"r-{i}", "type": "reply_sent", "mailbox": HANNAH, "contact_id": f"k-{i}",
                                 "approval": "approved", "occurred_at": RECENT + timedelta(hours=2, minutes=i)}
                                for i in range(30)])
    out = kill_rules.run(ctx)
    [fired] = out["fired"]
    assert fired["rule"] == "mailbox_bounce_rate" and "2 of its last 50 sends bounced (4.0%" in fired["reason"]


def test_one_bounce_never_pauses_a_mailbox():
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    sends(ctx, HANNAH, 10, bounced=1)  # 10% on the ramp's 10 a day, but one bounce
    out = kill_rules.run(ctx)
    assert out["fired"] == [] and out["suppressed"] == 1
    assert row(sheets, HANNAH)["status"] == "Active" and slack_posts(t) == []


def test_bounces_older_than_the_window_do_not_count():
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    sends(ctx, HANNAH, 40, bounced=4, at=NOW - timedelta(days=9))
    assert kill_rules.run(ctx)["fired"] == []


# -- block signals: 14 days ---------------------------------------------------------------------------------


def test_a_block_bounce_pauses_for_14_days_and_the_hold_ends_by_itself():
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    sends(ctx, SAM, 50, bounced=1, block="550 5.7.1 Message rejected: sender blocked")
    out = kill_rules.run(ctx)
    [fired] = out["fired"]
    assert (fired["rule"], fired["target"], fired["until"]) == ("block_bounce", SAM, "2026-11-10")
    assert "1 block bounce (5.7.1)" in fired["reason"]
    assert row(sheets, SAM)["status"] == "Paused"
    assert "after the hold ends on Tue 10 Nov" in slack_posts(t)[0].json["text"]
    # 14 days on, the hold lifts; the sheet still says Paused until Harry sets it Active.
    out = kill_rules.run(later(ctx, t, 14))
    assert [e["mailboxes"] for e in out["expired"]] == [[SAM]] and holds.held_mailboxes(ctx.store) == {}
    assert "The 14-day pause of sam@meetspill.org (a block bounce) is over" in slack_posts(t)[-1].json["text"]
    assert row(sheets, SAM)["status"] == "Paused"


def test_instantly_vitals_failing_pauses_for_14_days():
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    inst.accounts[HARRY2]["status"] = -3  # sending_error
    out = kill_rules.run(ctx)
    [fired] = out["fired"]
    assert (fired["rule"], fired["target"]) == ("vitals", HARRY2)
    assert "Instantly reports the account as sending_error" in fired["reason"]
    assert inst.by_name(C_HARRY)["email_list"] == [HARRY]  # off the sending list; Harry's other address sends on
    [post] = slack_posts(t)
    assert post.json["text"].splitlines()[1] == (
        "• harry@tryspill.org paused for 14 days: Instantly reports the account as sending_error. Harry's campaign "
        "goes on from harry@meetspill.org; nothing else changes. To resume once fixed: after the hold ends on Tue 10 "
        f"Nov (or clear it sooner with `railway ssh -- us-outbound killrules clear {fired['item_id']} --live`), set it "
        "Active on the Mailboxes tab.")


def test_the_alert_names_each_address_once_and_cites_no_spec():
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    sends(ctx, HANNAH, 40, bounced=1)
    sends(ctx, HARRY, 40, bounced=1)
    sends(ctx, SAM, 40, bounced=2)
    kill_rules.run(ctx)
    [post] = slack_posts(t)
    text = post.json["text"]
    assert "SPEC" not in text and "Harry, " not in text
    domain = next(line for line in text.splitlines() if "their domain" in line)
    assert domain.startswith("• hannah@meetspill.org, harry@meetspill.org paused: their domain meetspill.org: 4 of its 120 "
                             "sends in the last 7 days bounced (3.3%; the limit is 3%).")
    assert "Hannah's approved leads wait; Harry's campaign goes on from harry@tryspill.org; nothing else changes." in domain
    assert "set them Active on the Mailboxes tab" in domain
    for line in text.splitlines()[1:]:
        for box in (HANNAH, HARRY, SAM):
            assert line.count(box) <= 1, line


def test_a_spam_complaint_or_a_seed_inbox_in_spam_pauses_for_14_days():
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    sends(ctx, HANNAH, 5)
    ctx.store.insert("events", [
        {"event_id": "cmp-1", "type": "complained", "contact_id": f"c-hannah-meetspill-{RECENT:%m%d}-0", "step": 1,
         "occurred_at": RECENT + timedelta(hours=1)},
        {"event_id": "seed-1", "type": "seed_spam", "mailbox": SAM, "occurred_at": RECENT},
    ])
    rules = {f["target"]: f["rule"] for f in kill_rules.run(ctx)["fired"]}
    assert rules == {HANNAH: "spam_complaint", SAM: "seed_spam"}


# -- the domain and the source ---------------------------------------------------------------------------


def test_a_domain_over_3_percent_on_100_sends_pauses_its_mailboxes():
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    sends(ctx, HANNAH, 40, bounced=1)
    sends(ctx, HARRY, 40, bounced=1)
    sends(ctx, SAM, 40, bounced=2)  # 5%: its own rule
    out = kill_rules.run(ctx)
    by_rule = {f["rule"]: f for f in out["fired"]}
    assert by_rule["mailbox_bounce_rate"]["target"] == SAM
    assert by_rule["domain_bounce_rate"]["target"] == "meetspill.org"
    assert "4 of its 120 sends in the last 7 days bounced (3.3%; the limit is 3%)" in by_rule["domain_bounce_rate"]["reason"]
    assert set(out["paused"]) == {HANNAH, HARRY, SAM}
    assert inst.by_name(C_HARRY)["email_list"] == [HARRY2]  # tryspill.org sends on
    assert row(sheets, HARRY2)["status"] == "Active"


def test_an_email_source_over_3_percent_is_paused_until_checked_and_enrol_skips_it():
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    for box in (HANNAH, HARRY, SAM, HARRY2):  # 1 bounce in 35 each: no mailbox, domain or account-level rule
        sends(ctx, box, 25, bounced=1, source="clay")
        sends(ctx, box, 10, source="apollo", prefix=f"apollo-{box.split('@')[0]}-{box.split('@')[1][:3]}")
    out = kill_rules.run(ctx)
    [fired] = out["fired"]
    assert (fired["rule"], fired["target"], fired["action"]) == ("source_bounce_rate", "clay", "pause_source")
    assert holds.paused_sources(ctx.store) == {"clay": fired["reason"]}
    # enrol leaves out contacts the paused source found.
    ctx.store.insert("accounts", [{"account_id": "acc-x", "domain": "x.com", "status": "verified", "tier": "Priority",
                                   "industry": "Fintech"}])
    ctx.store.insert("contacts", [{"contact_id": "con-x", "account_id": "acc-x", "email": "ann@x.com",
                                   "email_status": "valid", "email_source": "clay", "person_state": "NY"}])
    cands, skipped = enrol.candidates(ctx, frozenset())
    assert cands == [] and skipped["email source paused by a kill rule"] == 1


# -- the industry group and the stop rule ---------------------------------------------------------------------


def test_an_industry_group_under_half_a_percent_after_400_delivered_stops_enrolling():
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    group = {"industry_group": "Marketing & Creative Agencies"}
    ids = sends(ctx, HANNAH, 400, at=NOW - timedelta(days=40), group_account=group)
    ctx.store.insert("events", [{"event_id": "r-1", "type": "replied", "contact_id": ids[0],
                                 "account_id": ids[0].replace("c-", "a-", 1), "reply_class": "negative",
                                 "occurred_at": NOW - timedelta(days=39)}])
    out = kill_rules.run(ctx)
    [fired] = out["fired"]
    assert (fired["rule"], fired["target"], fired["action"]) == (
        "group_reply_rate", "Marketing & Creative Agencies", "stop_group")
    assert "1 human replies from 400 accounts delivered (0.2%; the floor is 0.5%)" in fired["reason"]
    ctx.store.insert("accounts", [{"account_id": "acc-y", "domain": "y.com", "status": "verified", "tier": "Priority",
                                   "industry_group": "Marketing & Creative Agencies"}])
    _, skipped = enrol.candidates(ctx, frozenset())
    assert skipped["industry group stopped by a kill rule"] == 1


def test_the_stop_rule_pauses_enrolment_until_harry_clears_it():
    s = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, stop_rule_accounts=3,
                                                                  stop_rule_meetings=1))
    ctx, t, inst, sheets = setup(s)
    campaigns(inst)
    ctx.store.insert("contacts", [{"contact_id": f"k{i}", "account_id": f"acc{i}", "enrolled_at": NOW - timedelta(days=30 + i)}
                                  for i in range(3)])
    out = kill_rules.run(ctx)
    [fired] = out["fired"]
    assert (fired["rule"], fired["action"]) == ("stop_rule_meetings", "pause_enrolment")
    assert "the first 3 accounts brought 0 meetings, fewer than stop_rule_meetings (1)" in fired["reason"]
    from tests.fakes import make_context

    dry = make_context(s, store=ctx.store, now=ctx.now)  # enrol in dry-run: live_sending is no here
    assert enrol.gate(dry, dry.now_et().date()).startswith("enrollment is paused by the stop rule")
    kill_rules.clear(ctx, fired["item_id"], "harry")
    assert enrol.gate(dry, dry.now_et().date()) is None
    assert kill_rules.run(later(ctx, t, 1))["fired"] == []  # it reads the first accounts once


def test_the_first_accounts_reply_windows_must_close_first():
    s = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, stop_rule_accounts=3))
    ctx, t, inst, sheets = setup(s)
    ctx.store.insert("contacts", [{"contact_id": f"k{i}", "account_id": f"acc{i}", "enrolled_at": NOW - timedelta(days=5)}
                                  for i in range(3)])
    assert kill_rules.run(ctx)["fired"] == []


def test_account_level_bounces_raise_the_stop_rule():
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    for box in (HANNAH, HARRY, SAM, HARRY2):  # 12 days ago: outside the mailbox rules' 7 days
        sends(ctx, box, 25, bounced=1, at=NOW - timedelta(days=12))
    out = kill_rules.run(ctx)
    [fired] = out["fired"]
    assert (fired["rule"], fired["action"]) == ("stop_rule_bounce_rate", "pause_enrolment")
    assert "4 of the 100 accounts sent step 1 in the last 30 days bounced (4.0%; the limit, stop_rule_bounce_rate, is 3.0%)" \
        in fired["reason"]
    assert holds.enrolment_stop(ctx.store)


# -- dry-run, Slack and the holds ------------------------------------------------------------------------------


def test_dry_run_records_the_items_but_changes_neither_the_sheet_nor_instantly():
    ctx, t, inst, sheets = setup(live=False)
    campaigns(inst)
    sends(ctx, HANNAH, 40, bounced=2)
    out = kill_rules.run(ctx)
    assert out["dry_run"] and [f["rule"] for f in out["fired"]] == ["mailbox_bounce_rate"]
    assert out["paused"] == {HANNAH: "dry-run: not paused in the sheet or Instantly"}
    assert row(sheets, HANNAH)["status"] == "Active" and inst.by_name(C_HANNAH)["email_list"] == [HANNAH]
    assert ctx.guard.writes("instantly", sent=True) == [] and ctx.guard.writes("sheets", sent=True) == []
    [post] = slack_posts(t)
    assert post.json["channel"] == "C_DEV" and "dry-run: neither the sheet nor Instantly was changed" in post.json["text"]
    assert items(ctx, "open")[0]["payload"]["dry_run"] is True


def test_with_no_slack_token_the_alert_goes_to_the_log_and_the_pause_still_happens(capsys):
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    ctx.clients.secrets = Secrets(ctx.guard, fetch=lambda name: "" if "SLACK" in name else f"test-{name}")
    sends(ctx, HANNAH, 40, bounced=2)
    out = kill_rules.run(ctx)
    assert row(sheets, HANNAH)["status"] == "Paused"
    assert out["alert"] == {"posted": False, "channel": "#us-outbound", "ts": None,
                            "error": "no Slack token: posted to the log"}
    assert slack_posts(t) == [] and '"event": "slack_off"' in capsys.readouterr().out


def test_a_held_mailbox_never_goes_back_on_a_sending_list_before_the_sheet_syncs():
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    inst.accounts[HARRY2]["status"] = -3
    kill_rules.run(ctx)
    assert inst.by_name(C_HARRY)["email_list"] == [HARRY]
    # ctx.settings still says Active (the sheet syncs at 02:00); the campaign fix leaves it off.
    assert [m.status for m in ctx.settings.mailboxes if m.address == HARRY2] == ["Active"]
    out = reg.ensure_campaigns(ctx, fix=True)
    assert C_HARRY not in out["drift"] and inst.by_name(C_HARRY)["email_list"] == [HARRY]


def test_the_bootstrap_settings_show_held_mailboxes_as_paused():
    from us_outbound.clients.db import MemoryStore
    from us_outbound.clients.guard import Guard
    from us_outbound.ops import bootstrap

    store = MemoryStore(Guard())
    store.insert("hitl_items", [{"item_id": "k1", "kind": "kill_rule", "status": "open",
                                 "payload": {"action": "pause_mailbox", "mailboxes": [SAM], "reason": "test"}}])
    import us_outbound.settings.sync as sync

    original = sync.load_current
    sync.load_current = lambda st: (SETTINGS, {})
    try:
        settings, _ = bootstrap._load_settings(store)
    finally:
        sync.load_current = original
    assert {m.address: m.status for m in settings.mailboxes}[SAM] == "Paused"


# -- the killrules command --------------------------------------------------------------------------------


def test_killrules_show_and_clear(capsys):
    from tests.test_cli import Harness

    h = Harness()
    h.store.insert("hitl_items", [{"item_id": "k1", "kind": "kill_rule", "status": "open", "created_at": NOW,
                                   "payload": {"rule": "source_bounce_rate", "action": "pause_source", "target": "clay",
                                               "reason": "emails found by clay: 4 of 100 bounced"}}])
    assert h.run("killrules", "show") == 0
    out = capsys.readouterr().out
    assert "k1  source_bounce_rate: pause_source clay" in out and "4 of 100 bounced" in out
    assert h.run("killrules", "clear", "k1") == 0  # dry-run: the hold stays
    assert holds.paused_sources(h.store) == {"clay": "emails found by clay: 4 of 100 bounced"}
    assert h.run("killrules", "clear", "k1", "--live") == 0
    assert holds.paused_sources(h.store) == {} and h.store.get("hitl_items", item_id="k1")["status"] == "handled"
    assert h.run("killrules", "clear", "nope", "--live") == 2


def test_kill_rules_runs_as_a_job_from_the_cli():
    from tests.test_cli import Harness

    h = Harness()
    assert h.run("dry-run", "kill_rules") == 0
    [beat] = h.beats("kill_rules")
    assert beat["status"] == "ok" and beat["detail"]["fired"] == []
