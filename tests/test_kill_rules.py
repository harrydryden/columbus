"""kill_rules (SPEC 12 "Kill rules"; learn/kill_rules.py, base/holds.py): the rules, the registry pause
path, suppression, the holds enrol and the registry honour, the alert, and the killrules command."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

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
from us_outbound.base import holds, notify
from us_outbound.context import Secrets
from us_outbound.enrol import capacity, enrol
from us_outbound.learn import kill_rules
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


MAILBOXES_TAB = "<https://docs.google.com/spreadsheets/d/sheet-test/edit|Mailboxes tab>"


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
    assert post.json["text"].splitlines() == [
        "<@U_HARRY> :rotating_light: *Safety stop: Hannah's mailbox has stopped sending*",
        "hannah@meetspill.org: 2 of its last 40 emails bounced (5.0%; the limit is 3%).",
        "• Hannah's campaign is paused: none of Hannah's contacts get a first email or a follow-up until one of "
        "Hannah's mailboxes is Active again.",
        f"• To resume once the cause is fixed: set it to Active on the {MAILBOXES_TAB}, then run "
        f"`railway ssh -- us-outbound killrules clear {fired['item_id']} --live`. The next settings sync and mailbox "
        "check (07:00 UK) put it back on the sending list; for sooner, run `railway ssh -- us-outbound sync --live`, "
        "then `railway ssh -- us-outbound mailbox check --live`.",
        "*What to do:* Find out the cause, then follow the step above to resume. Until then it stays paused.",
        "_Why it matters: too many bounces or spam complaints make mail providers treat all our email as spam._",
    ]
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


def _rules(out, rule):
    return [f for f in out["fired"] if f["rule"] == rule]


def test_K2_exactly_3_percent_is_not_over_3_percent():
    """tests-mutation-safety-measure K2: SPEC 12 says more than 3%; 3 bounces in 100 sends would pause a healthy
    mailbox under ">= 3%"."""
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    sends(ctx, HANNAH, 100, bounced=3)
    assert _rules(kill_rules.run(ctx), "mailbox_bounce_rate") == []
    assert row(sheets, HANNAH)["status"] == "Active"


def test_K3_a_mailbox_is_judged_on_its_latest_100_sends():
    """tests-mutation-safety-measure K3: past the ramp a mailbox sends over 100 a week; it is judged on its latest
    100 in the window. 150 sends with the 4 oldest bounced: the latest 100 are clean, so nothing fires. The same 150
    with the 4 latest bounced: 4 of the latest 100 (4%, over 3% and at least 2) fires."""
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    sends(ctx, HANNAH, 150, bounced=4)  # the 4 oldest bounced
    assert _rules(kill_rules.run(ctx), "mailbox_bounce_rate") == []

    ctx, t, inst, sheets = setup()
    campaigns(inst)
    ids = sends(ctx, HANNAH, 150)
    ctx.store.insert("events", [{"event_id": f"b-{cid}", "type": "bounced", "contact_id": cid, "step": 1,
                                 "occurred_at": RECENT + timedelta(minutes=200), "reply_text": "550 5.1.1 user unknown"}
                                for cid in ids[-4:]])
    [fired] = _rules(kill_rules.run(ctx), "mailbox_bounce_rate")
    assert fired["target"] == HANNAH and "4 of" in fired["reason"]


def test_K5_the_group_rule_waits_for_the_28_day_reply_windows_to_close():
    """tests-mutation-safety-measure K5: 400 step-1 sends 10 days ago with no reply must not stop an industry group:
    its reply windows are open (the 40-day case above fires)."""
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    sends(ctx, HANNAH, 400, at=NOW - timedelta(days=10), group_account={"industry_group": "Marketing & Creative Agencies"})
    assert _rules(kill_rules.run(ctx), "group_reply_rate") == []


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
    text = slack_posts(t)[0].json["text"]
    assert text.startswith("<@U_HARRY> :rotating_light: *Safety stop: Sam's mailbox has stopped sending for 14 days*\n"
                           "sam@meetspill.org: a receiving mail server refused its email (1 block bounce, code 5.7.1).")
    assert "the stop lifts by itself after Tue 10 Nov (or sooner with `railway ssh -- us-outbound killrules" in text
    # 14 days on, the hold lifts; the sheet still says Paused until Harry sets it Active.
    out = kill_rules.run(later(ctx, t, 14))
    assert [e["mailboxes"] for e in out["expired"]] == [[SAM]] and holds.held_mailboxes(ctx.store) == {}
    head, line, do = slack_posts(t)[-1].json["text"].splitlines()
    assert head == "<@U_HARRY> :warning: *Safety stop over: a mailbox can send again*"
    assert line == ("• Sam's mailbox sam@meetspill.org: the 14-day pause after a block bounce is over, but the Mailboxes tab "
                    "still says Paused, so it sends nothing yet.")
    assert do.startswith(f"*What to do:* Once you are happy the cause is fixed, set it to Active on the {MAILBOXES_TAB}.")
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
    lines = post.json["text"].splitlines()
    assert lines[0] == "<@U_HARRY> :rotating_light: *Safety stop: Harry's mailbox has stopped sending for 14 days*"
    assert lines[1] == "harry@tryspill.org: Instantly reports the account as sending_error."
    assert lines[2] == "• Harry's campaign carries on from harry@meetspill.org: first emails and follow-ups still go."
    assert lines[3].startswith(
        "• To resume once the cause is fixed: the stop lifts by itself after Tue 10 Nov (or sooner with "
        f"`railway ssh -- us-outbound killrules clear {fired['item_id']} --live`); then set it to Active on the "
        f"{MAILBOXES_TAB}.")


def test_the_group_and_stop_rule_alerts_say_what_still_goes_and_how_to_resume():
    ctx, t, inst, sheets = setup()
    group = kill_rules.Firing("group_reply_rate", "industry_group", "Fintech", holds.STOP_GROUP, "Fintech: 1 human ...",
                              numbers={"delivered": 400, "replied": 1, "rate": 0.0025}, item_id="g-1")
    assert kill_rules.message(ctx, [group], []).splitlines() == [
        "<@U_HARRY> :rotating_light: *Safety stop: no new Fintech companies are being emailed*",
        "Why: only 1 of the 400 companies emailed in it replied (0.2%; the minimum is 0.5%).",
        "• Companies already emailed carry on: their follow-ups still go.",
        "• To add Fintech companies again: `railway ssh -- us-outbound killrules clear g-1 --live`.",
        "*What to do:* Find out the cause, then follow the step above to resume. Until then it stays paused.",
        "_Why it matters: too many bounces or spam complaints make mail providers treat all our email as spam._",
    ]
    stop = kill_rules.Firing("stop_rule_meetings", "enrolment", "stop_rule", holds.PAUSE_ENROLMENT, "the first ...",
                             numbers={"accounts": 100, "meetings": 1}, item_id="s-1")
    lines = kill_rules.message(ctx, [stop], []).splitlines()
    assert lines[0] == "<@U_HARRY> :rotating_light: *Safety stop: no new companies are being emailed*"
    assert lines[1].startswith("Why: the first 100 companies emailed brought 1 meeting, fewer than `stop_rule_meetings` (")
    assert lines[2] == "• Emails already under way carry on: their follow-ups still go."


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
    assert text.splitlines()[0] == ("<@U_HARRY> :rotating_light: *Safety stop: 4 things paused to protect our email "
                                    "reputation*")
    assert ("*2 mailboxes on meetspill.org (hannah@meetspill.org for Hannah, harry@meetspill.org for Harry) have stopped "
            "sending* because 4 of the 120 emails sent from meetspill.org in the last 7 days bounced (3.3%; the limit is "
            "3%).") in text.splitlines()
    assert "• Harry's campaign carries on from harry@tryspill.org: first emails and follow-ups still go." in text
    assert f"set them to Active on the {MAILBOXES_TAB}" in text
    # The source and stop-rule lines (a source and the stop rule fire on the same sends here).
    assert ("*New contacts whose email address came from Apollo are no longer added to the campaign* because 4 of the "
            "120 emails sent to these addresses in the last 7 days bounced (3.3%; the limit is 3%).") in text.splitlines()
    assert "• Contacts already in the campaign carry on: their follow-ups still go. Contacts found by Clay are still added." \
        in text.splitlines()
    assert ("*No new companies are added to the campaign at all (the stop rule)* because 4 of the 120 companies first "
            "emailed in the last 30 days bounced (3.3%; the limit, `stop_rule_bounce_rate` on the General tab, is "
            "3.0%).") in text.splitlines()
    assert "*What to do:* Find out the cause, then follow the steps above to resume. Until then they stay paused." in text
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
    assert post.json["channel"] == "C_DEV"
    assert "*Safety stop: Hannah's mailbox would stop sending*" in post.json["text"]
    assert "• Hannah's campaign would be paused: none of Hannah's contacts get a first email" in post.json["text"]
    assert "• Nothing was changed in the sheet or Instantly; only the stop is recorded. To lift it: " in post.json["text"]
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


def _slack_times_out(t, times: int = 1):
    """The next `times` posts raise ReadTimeout (an OSError: the POST may have gone), then posts go through."""
    import requests

    left = [times]

    def post(req):
        if left[0] > 0:
            left[0] -= 1
            raise requests.exceptions.ReadTimeout("read timed out")
        return {"ok": True, "channel": req.json["channel"], "ts": "2.2"}

    t.route("POST", "chat.postMessage", fn=post)


def test_an_alert_lost_to_a_slack_timeout_is_posted_again_from_the_outbox():
    """W1-1 (code-platform-ops B2): the pause comes before the alert, and a Slack ReadTimeout lost the alert for good
    while the mailbox stayed paused. Run 1: paused, the alert times out, the item keeps slack_ts empty. Run 2 (an
    hour on, Slack healthy): exactly one post, slack_ts set. Run 3: nothing more."""
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    sends(ctx, HANNAH, 40, bounced=2)
    _slack_times_out(t)
    out = kill_rules.run(ctx)
    assert out["alert"]["posted"] is False and out["alert"]["error"] == "unknown outcome: ReadTimeout"
    [item] = items(ctx, "open")
    assert item.get("slack_ts") is None and item["payload"]["alert_attempts"] == 1 and item["payload"]["outbox"] is True
    assert row(sheets, HANNAH)["status"] == "Paused" and inst.by_name(C_HANNAH)["status"] == 2  # the pause stood
    run2 = later(ctx, t, 1 / 24)
    out2 = kill_rules.run(run2)
    assert out2["fired"] == [] and out2["reposted"] == {"items": [item["item_id"]], "posted": True, "error": None}
    [post] = [p for p in slack_posts(t) if p.json.get("text")][-1:]
    assert "Safety stop: Hannah's mailbox has stopped sending" in post.json["text"]  # the same alert, posted again
    assert "Posted again" in post.json["text"]
    [item] = items(ctx, "open")
    assert item["slack_ts"] == "2.2" and item["payload"]["alert_attempts"] == 2
    posts_so_far = len(slack_posts(t))
    assert kill_rules.run(later(ctx, t, 2 / 24))["reposted"] is None and len(slack_posts(t)) == posts_so_far


def test_the_outbox_skips_dry_run_items_old_items_and_backs_off_after_three_tries():
    ctx, t, inst, sheets = setup(live=False)
    campaigns(inst)
    sends(ctx, HANNAH, 40, bounced=2)
    _slack_times_out(t)
    kill_rules.run(ctx)  # dry run: its alert to the dev channel times out
    [dry] = items(ctx, "open")
    assert dry.get("slack_ts") is None and dry["payload"]["dry_run"] is True
    live = later(ctx, t, 1 / 24)
    live.guard.configure(live=True)
    base = {"rule": "vitals", "scope": "mailbox", "target": SAM, "action": "pause_mailbox", "reason": "sending_error",
            "mailboxes": [SAM], "until": None, "numbers": {}, "dry_run": False, "outcome": "", "campaigns_paused": [],
            "outbox": True}
    ctx.store.upsert("hitl_items", [
        # 8 days old: past OUTBOX_DAYS (7), never posted again.
        {"item_id": "old", "kind": "kill_rule", "status": "open", "created_at": ctx.now - timedelta(days=8),
         "payload": {**base, "alert_attempts": 1}},
        # tried 3 times (OUTBOX_TRIES): waits for the 09:00 UK run.
        {"item_id": "tried", "kind": "kill_rule", "status": "open", "created_at": ctx.now - timedelta(hours=3),
         "payload": {**base, "alert_attempts": 3}},
        # written before the outbox existed (no outbox key): never posted again.
        {"item_id": "before", "kind": "kill_rule", "status": "open", "created_at": ctx.now - timedelta(hours=3),
         "payload": {k: v for k, v in base.items() if k != "outbox"}},
        # its alert went to the log for want of a Slack token: trying again would only log it again.
        {"item_id": "no-token", "kind": "kill_rule", "status": "open", "created_at": ctx.now - timedelta(hours=3),
         "payload": {**base, "alert_attempts": 1, "alert_error": notify.NO_TOKEN}},
    ])
    assert [r["item_id"] for r in kill_rules.unsent(live)] == []  # 13:00 UK: none is due
    nine = later(ctx, t, 20 / 24)  # Wed 28 Oct 08:00 UTC = 08:00 UK (GMT)
    nine.now = nine.now.replace(hour=9)
    nine.guard.configure(live=True)
    assert [r["item_id"] for r in kill_rules.unsent(nine)] == ["tried"]
    assert kill_rules.post_unsent(ctx) == {}  # a dry run posts nothing again


def test_an_outbox_item_that_cannot_be_read_never_stops_a_new_pause():
    """The outbox runs first in kill_rules.run; an item whose payload was edited by hand (numbers "see Slack", which
    _firing cannot make a mapping of) used to abort the run before any new firing. Now the run goes on: Hannah's 2
    bounces in 40 (5.0% > 3%) still pause her mailbox, and the summary names the outbox's error."""
    ctx, t, inst, sheets = setup()
    campaigns(inst)
    ctx.store.upsert("hitl_items", [{"item_id": "edited", "kind": "kill_rule", "status": "open",
                                     "created_at": ctx.now - timedelta(hours=2), "payload": {
        "rule": "vitals", "scope": "mailbox", "target": SAM, "action": "pause_mailbox", "reason": "sending_error",
        "mailboxes": [SAM], "until": None, "numbers": "see Slack", "dry_run": False, "outbox": True}}])
    assert [r["item_id"] for r in kill_rules.unsent(ctx)] == ["edited"]
    sends(ctx, HANNAH, 40, bounced=2)
    out = kill_rules.run(ctx)
    assert [f["rule"] for f in out["fired"]] == ["mailbox_bounce_rate"]
    assert row(sheets, HANNAH)["status"] == "Paused" and inst.by_name(C_HANNAH)["status"] == 2
    assert out["reposted"] is None and out["reposted_error"].startswith("ValueError")


def test_a_guard_refusal_in_the_outbox_is_not_swallowed(monkeypatch):
    from us_outbound.clients.guard import GuardViolation

    ctx, t, inst, sheets = setup()

    def refused(_ctx):
        raise GuardViolation("not allowed")

    monkeypatch.setattr(kill_rules, "post_unsent", refused)
    with pytest.raises(GuardViolation):
        kill_rules.run(ctx)


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
