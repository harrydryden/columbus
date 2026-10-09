"""The weekly hand-check (SPEC 11 "Weekly hand-check"; SPEC 14 phase 1 acceptance): Harry checks a
random sample of the queue before that week's enrollment, and enrol waits until he has.

hand_check_post, Mondays 08:00 UK and on demand (`us-outbound run hand_check_post`):
  1. picks PER_GROUP (10, SPEC 11) random queued or verified accounts from each active
     industry group (a group with an active industry on the Industries tab), verified ones
     first since they are the next to be emailed. Accounts enrol would never take are left out:
     Held or Excluded tiers, suppressed or partner domains, industries switched off. The draw
     is seeded with the ISO week, so it is the same draw however often it runs that week;
  2. records the week's item in hitl_items (kind hand_check, item_id "hand_check-{iso_week}",
     status open), its payload holding iso_week, the account ids by group, and for each
     account the facts Harry checks: clean name, HQ state, size band, the role and title of
     the contact enrol would choose, and the opener with its evidence (SPEC 14: "Clean name,
     HQ state, size band, role title and opener evidence are right in 90% or more"). The
     opener is the one enrol would give that contact (enrol/openers.py), worked out with no
     model call; a held-out account shows none, with the line it would have had;
  3. adds the accounts verify_accounts held back for doubtful Apollo facts (verify.open_doubts:
     no HQ state, a size near a band edge, and so on; Harry, 2 Oct 2026; with clay_cross_check = yes,
     only what Clay could not settle, as "Clay says 62 staff, Apollo says 49"), each with its reasons,
     in payload.doubtful, and the site visitors held there (sources/site_visits.py; Harry, 6 Oct 2026).
     They are not verified until this hand-check is approved: approving clears the doubts of each
     one not pulled, and the next verify_accounts run decides on its facts as usual (an Overrides
     row corrects one that is wrong). A missing fact (no HQ state, size or industry) is not cleared
     by approving: the account stays on the check until an Overrides row fills it in. A pulled one
     stays held;
  4. posts it to the alert channel (in dry-run, the dev channel), or logs it with no Slack
     token, in plain words: what to look at, the command that approves it, and what waits
     meanwhile. A live run posts an item a dry run recorded unposted.
A week with nothing to check records no item: run it again once accounts are queued.
Doubts found after the week's item is recorded wait for the next week's hand-check.

With the General key auto_send = no (the default; Harry, 2 Oct 2026) every email is approved in
Slack before it is sent (enrol/approvals.py), so there is no random sample and enrol does not wait
for the hand-check. The accounts held for doubtful Apollo facts are not covered by that (they are
not verified, so no email of theirs is proposed), so a week with any of them still records and posts
an item holding only those (per_group 0); a week without any skips, saying why. If auto_send is then
switched to yes, that item does not pass enrol's gate (enrol.has_sample): hand_check_post records the
week's item again with the sample, keeping its pulls, to be approved again, and `handcheck approve`
does the same itself.

Without Slack (Harry, 1 Oct 2026; operator commands, live with --live alone):
  us-outbound handcheck show               prints this week's check; it never writes
  us-outbound handcheck approve [--pull ACCOUNT_ID ...] [--live]
                                           marks the week's item handled, with the pulled
                                           accounts (ids or domains) in payload.pulled_account_ids,
                                           which enrol.hand_check leaves out (enrol.account_block).
                                           With no item yet it records the week's draw first: the
                                           one `show` printed, as the draw is seeded with the week
enrol.hand_check reads the item: enrollment waits until every hand_check item of the ISO week
(US Eastern date, as enrol counts it) is handled.
docs/gtm-review/README.md §4.1 B4 suggests 5 accounts a group to fit Harry's hours; PER_GROUP
is SPEC 11's 10 until Harry decides.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from typing import Any

from us_outbound import accounts, labels, verify
from us_outbound.context import Context
from us_outbound.enrol import enrol, openers, queue, render
from us_outbound.logs import log
from us_outbound.ops import notify
from us_outbound.scoring.score import score_account
from us_outbound.settings.model import Settings

JOB = "hand_check_post"
KIND = "hand_check"
PER_GROUP = 10  # SPEC 11: ten random queued accounts per active industry group
STATUSES = ("verified", "queued")  # verified first: they are the next to be emailed
EVIDENCE_LIMIT = 3
QUOTE_LIMIT = 160


AUTO_SEND_OFF = ("auto_send = no: every email is approved in Slack before it is sent, so there is no weekly "
                 "sample (Harry, 2 Oct 2026)")


def item_id(week: str) -> str:
    return f"{KIND}-{week}"


def sample_size(ctx: Context) -> int:
    """Accounts drawn per active group: PER_GROUP, or none while every email is approved in Slack (auto_send = no)."""
    return PER_GROUP if ctx.settings.general.auto_send else 0


def this_week(ctx: Context) -> str:
    """The ISO week enrol checks: of today's US Eastern date."""
    return enrol.iso_week(ctx.now_et().date())


def _clip(text: Any, n: int = QUOTE_LIMIT) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def active_groups(settings: Settings) -> list[str]:
    """Industry groups with at least one active industry, in Industries-tab order."""
    return list(settings.active_groups())


# -- the sample ---------------------------------------------------------------------------------


def _pool(ctx: Context) -> dict[str, list[dict]]:
    """group -> the accounts enrol could take this week: queued or verified, in a queue tier, not blocked."""
    s = ctx.settings
    groups = {g.casefold(): g for g in active_groups(s)}
    domains, _ = enrol.suppressed(ctx)
    partners = {str(p.get("domain") or "").strip().lower() for p in ctx.store.select("partners")}
    out: dict[str, list[dict]] = {g: [] for g in groups.values()}
    for a in ctx.store.select("accounts", {"status": list(STATUSES), "tier": list(queue.QUEUE_TIERS)}):
        group = groups.get(s.industry_group_of(a).casefold())
        if group is None or enrol.account_block(a, s, domains, partners, frozenset()) is not None:
            continue
        out[group].append(a)
    return out


def draw(ctx: Context, week: str, per_group: int = PER_GROUP) -> dict[str, list[dict]]:
    """group -> its random sample this week: verified accounts first, then queued, seeded by the week."""
    out: dict[str, list[dict]] = {}
    for group, accounts in _pool(ctx).items():
        rng = random.Random(f"{week}:{group}")
        picked: list[dict] = []
        for status in STATUSES:
            these = sorted((a for a in accounts if a.get("status") == status), key=lambda a: str(a["account_id"]))
            room = per_group - len(picked)
            if room > 0 and these:
                picked += rng.sample(these, min(room, len(these)))
        if picked:
            out[group] = picked
    return out


def _person(ctx: Context, account_id: str, domains: set[str], hashes: set[str]) -> tuple[dict, str]:
    """(the contact enrol would choose, or the first on file, or {}; why it cannot be emailed yet, or "")."""
    contacts = ctx.store.select("contacts", {"account_id": account_id})
    chosen, why = enrol.pick_contact(contacts, domains, hashes)
    if chosen is not None:
        return chosen, ""
    if contacts:
        first = sorted(contacts, key=lambda c: (str(c.get("created_at") or ""), str(c.get("contact_id"))))[0]
        return dict(first), f"not sendable yet: {why}"
    return {}, "no contact yet (pick_contacts finds one before enrollment)"


def opener_for(ctx: Context, account: Mapping[str, Any], contact: Mapping[str, Any],
               events: Sequence[Mapping[str, Any]]) -> openers.Opener:
    """The opener enrol would give this contact (enrol/openers.py), with no model call: a "what they do"
    phrase only if one is stored already."""
    exempt = (str(account.get("clean_name") or ""), str(contact.get("first_name") or ""))

    def check(text: str) -> str:
        return render.pick_opener(text, sender_is_harry=False, demo_host=ctx.settings.general.demo_host,
                                  exempt=exempt)[1]

    return openers.for_account(ctx, account, contact, events, check=check, spend=False)


def facts(ctx: Context, account: Mapping[str, Any], domains: set[str], hashes: set[str]) -> dict[str, Any]:
    """What Harry checks for one account (SPEC 14 phase 1 acceptance), the opener as enrol would send it."""
    events = ctx.store.select("signal_events", {"account_id": account["account_id"]})
    r = score_account(account, events, ctx.settings, ctx.today_uk())
    person, note = _person(ctx, str(account["account_id"]), domains, hashes)
    op = opener_for(ctx, account, person, events)
    matches = sorted((m for m in r.matches if m.signal.action == "Score"), key=lambda m: -m.weight_applied)
    evidence = []
    for m in matches[:EVIDENCE_LIMIT]:
        ev = m.evidence[0] if m.evidence else None
        evidence.append({"signal": m.signal.signal, "weight": m.weight_applied, "text": _clip(ev.text if ev else ""),
                         "quote": _clip(ev.quote if ev else ""), "url": (ev.url if ev else "") or ""})
    return {
        "account_id": account["account_id"], "domain": account.get("domain") or "", "clean_name": account.get("clean_name") or "",
        "hq_city": account.get("hq_city") or "", "hq_state": account.get("hq_state") or "",
        "size_band": account.get("size_band") or "", "employees": account.get("employees"),
        "industry": account.get("industry") or "", "industry_group": ctx.settings.industry_group_of(account),
        "tier": account.get("tier") or "", "score": account.get("score"), "status": account.get("status") or "",
        "angle": account.get("angle") or "",
        "contact": {"role": person.get("role") or "", "title": person.get("title") or "", "note": note},
        "opener": _clip(op.text or op.would_be, 300), "opener_arm": op.arm, "opener_source": op.source,
        "evidence": evidence,
        # The label check (labels.py; Harry, 7 Oct 2026): where the label came from and what the model read.
        "label_source": account.get("label_source") or "", "label_confidence": account.get("label_confidence") or "",
        "what_they_do": str((labels.latest_verdict(events) or {}).get("what_they_do") or ""),
    }


def build(ctx: Context, week: str, per_group: int = PER_GROUP) -> dict[str, Any]:
    """The week's payload: the draw and each account's facts, and the accounts held for doubtful facts."""
    domains, hashes = enrol.suppressed(ctx)
    sample = draw(ctx, week, per_group)
    return {
        "iso_week": week, "per_group": per_group,
        "groups": {g: [a["account_id"] for a in accts] for g, accts in sample.items()},
        "accounts": [facts(ctx, a, domains, hashes) for accts in sample.values() for a in accts],
        "doubtful": verify.open_doubts(ctx),
        "pulled_account_ids": [], "dry_run": ctx.dry_run, "posted": False,
    }


def has_work(payload: Mapping[str, Any]) -> bool:
    """Whether the payload has anything for Harry to check."""
    return bool(payload.get("accounts") or payload.get("doubtful"))


# -- the text Harry reads ------------------------------------------------------------------------------


def _where(a: Mapping[str, Any]) -> str:
    return ", ".join(x for x in (a.get("hq_city"), a.get("hq_state")) if x) or "HQ unknown"


def _who(a: Mapping[str, Any]) -> str:
    c = a.get("contact") or {}
    who = ", ".join(x for x in (c.get("role"), c.get("title")) if x) or "no contact"
    return who + (f" ({c['note']})" if c.get("note") else "")


def _size(a: Mapping[str, Any]) -> str:
    band = a.get("size_band") or "size unknown"
    return f"{band} ({a['employees']} staff)" if a.get("employees") not in (None, "") else band


def _opener(a: Mapping[str, Any]) -> str:
    """The opener as enrol would send it; a held-out account sends none, and shows the line it would have had.

    The generic line (General opener_generic_*) says nothing about the account, so it is marked: there is
    no evidence for Harry to check behind it."""
    line = a.get("opener") or ""
    if line and str(a.get("opener_source") or "").startswith(openers.GENERIC_OPENER_KEY):
        line += " (the generic line)"
    if a.get("opener_arm") == openers.HOLDOUT:
        return f"none (held out; would be: {line})" if line else "none (held out)"
    return line or "none"


def _label(a: Mapping[str, Any]) -> str:
    """" (rules+model: payroll software for restaurants)": how the label check placed it, when it has."""
    source, phrase = a.get("label_source") or "", a.get("what_they_do") or ""
    return f" ({': '.join(x for x in (source, phrase) if x)})" if source or phrase else ""


def _evidence(a: Mapping[str, Any]) -> str:
    parts = []
    for e in a.get("evidence") or ():
        text = f"{e['signal']}: “{e['quote'] or e['text']}”"
        parts.append(text + (f" ({e['url']})" if e.get("url") else ""))
    return "; ".join(parts) or "no signal evidence"


def week_label(week: Any) -> str:
    """"2026-W41" -> "Week 41"."""
    number = str(week or "").rpartition("W")[2].lstrip("0")
    return f"Week {number}" if number.isdigit() else f"Week {week}"


def _accounts(n: int) -> str:
    return f"{n} account{'' if n == 1 else 's'}"


def text(payload: Mapping[str, Any], *, detailed: bool = True) -> str:
    """The week's check as Harry reads it: in Slack (compact) or from `handcheck show` (detailed). Plain words:
    what to look at, how to approve, and what waits meanwhile."""
    by_id = {a["account_id"]: a for a in payload.get("accounts") or ()}
    doubtful = payload.get("doubtful") or []
    sample = enrol.has_sample(payload)
    week = week_label(payload.get("iso_week"))
    if not sample:  # auto_send = no: only the accounts held for doubtful facts
        k = len(doubtful)
        lines = [f"{week} check: {_accounts(k)} {'has' if k == 1 else 'have'} doubtful Apollo facts, so "
                 f"{'it' if k == 1 else 'they'} won't get a card until you look."]
    else:
        drawn = sum(len(ids) for ids in (payload.get("groups") or {}).values())
        also = f", and {_accounts(len(doubtful))} with doubtful Apollo facts" if doubtful else ""
        lines = [f"{week} check: {_accounts(drawn)} drawn at random from the queue{also}. Is each one right: the name, "
                 "HQ state, size band, the contact's role and title, and what the opener rests on?"]
    n = 0
    for group, ids in (payload.get("groups") or {}).items():
        lines.append(f"{group} ({len(ids)}):")
        for aid in ids:
            a = by_id.get(aid, {"account_id": aid})
            n += 1
            if detailed:
                lines += [
                    f"  {n}. {a.get('clean_name') or '?'} ({a.get('domain')})  id {aid}",
                    f"     {_where(a)} · {_size(a)} · {a.get('industry')}{_label(a)} · {a.get('tier')} {a.get('score')} · "
                    f"{a.get('status')}",
                    f"     Contact: {_who(a)}",
                    f"     Opener ({a.get('angle') or 'no angle'}): {_opener(a)}",
                    f"     Evidence: {_evidence(a)}",
                ]
            else:
                lines.append(f"• {a.get('clean_name') or '?'} ({a.get('domain')}) · {a.get('hq_state') or '?'} · "
                             f"{a.get('size_band') or '?'} · {_who(a)} · {_evidence(a)} · id {aid}")
    if doubtful:
        if sample:
            lines.append(f"Doubtful Apollo facts ({len(doubtful)}): not verified until you look.")
        for d in doubtful:
            n += 1
            visited = " · visited the US site" if d.get("source") == accounts.SITE_VISIT else ""
            lines.append(f"  {n}. {d.get('clean_name') or '?'} ({d.get('domain')}){visited} · "
                         f"HQ {d.get('hq_state') or '?'} · {_size(d)} · {'; '.join(d.get('reasons') or ())} · "
                         f"id {d.get('account_id')}")
        if any(verify.MISSING & set(d.get("reasons") or ()) for d in doubtful):
            lines.append("A missing fact needs an Overrides row (hq_state, employees or industry): approving alone "
                         "keeps the account on this check.")
    pulled = payload.get("pulled_account_ids") or []
    if pulled:
        lines.append(f"Pulled: {', '.join(pulled)}")
    example = next((str(a.get("domain")) for a in [*doubtful, *(payload.get("accounts") or ())] if a.get("domain")),
                   "acme.com")
    command = "us-outbound handcheck approve --live" if detailed else "railway ssh -- us-outbound handcheck approve --live"
    meanwhile = ("Until then no new leads go to Instantly this week." if sample
                 else "Ignoring this is safe: only these accounts wait.")
    relabel = f"{'' if detailed else 'railway ssh -- '}us-outbound labels set {example} \"<label>\" --live"
    lines.append(f"All fine: `{command}`. Any wrong: add `--pull {example}`, or fix it on the Overrides tab (a wrong "
                 f"industry: `{relabel}`). {meanwhile}")
    return "\n".join(lines)


# -- the job and the commands ------------------------------------------------------------------------


def current(ctx: Context, week: str | None = None) -> dict | None:
    """This week's hand_check item, if one is recorded."""
    week = week or this_week(ctx)
    rows = [r for r in ctx.store.select("hitl_items", {"kind": KIND}) if enrol._item_week(r) == week]
    if not rows:
        return None
    return next((r for r in rows if r.get("item_id") == item_id(week)), rows[0])


def _record(ctx: Context, payload: Mapping[str, Any]) -> dict:
    """The week's item, open (a rebuilt one replaces the one before it, approved or not)."""
    row = {"item_id": item_id(payload["iso_week"]), "kind": KIND, "status": "open", "created_at": ctx.now,
           "handled_at": None, "handled_by": None, "payload": dict(payload)}
    ctx.store.upsert("hitl_items", [row])
    return row


def needs_sample(ctx: Context, item: Mapping[str, Any] | None) -> bool:
    """While auto_send is yes the week's item must hold the random sample; one recorded while auto_send was no
    holds only the doubtful accounts (enrol.has_sample), so it is rebuilt with the sample."""
    return bool(item) and ctx.settings.general.auto_send and not enrol.has_sample((item or {}).get("payload"))


def _with_sample(ctx: Context, week: str, before: Mapping[str, Any] | None) -> dict[str, Any]:
    """The week's payload, drawn now (the same draw all week: it is seeded with the week). Pulls already made on
    an earlier item of the week are kept."""
    payload = build(ctx, week, sample_size(ctx))
    pulled = list(((before or {}).get("payload") or {}).get("pulled_account_ids") or [])
    if pulled:
        payload["pulled_account_ids"] = pulled
    return payload


def _post(ctx: Context, item: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(item.get("payload") or {})
    sent = notify.alert(ctx, notify.mention(ctx) + text(payload, detailed=False))
    payload["posted"] = bool(sent.get("posted")) and ctx.live
    update: dict[str, Any] = {"item_id": item["item_id"], "payload": payload}
    if sent.get("ts"):
        update.update(slack_channel=sent.get("channel"), slack_ts=sent["ts"])
    ctx.store.upsert("hitl_items", [update])
    return sent


def post(ctx: Context) -> dict:
    """The hand_check_post job (JOB CONTRACT: run(ctx) -> summary)."""
    week = this_week(ctx)
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run, "iso_week": week}
    item = current(ctx, week)
    if needs_sample(ctx, item):  # recorded while auto_send was no: rebuilt with the sample, to be approved again
        payload = _with_sample(ctx, week, item)
        if has_work(payload):
            item = _record(ctx, payload)
            sent = _post(ctx, item)
            summary.update(item_id=item["item_id"], status="recorded again with the sample (auto_send is yes now)",
                           accounts=len(payload["accounts"]), doubtful=len(payload["doubtful"]), alert=sent)
            log("hand_check_post", **{k: v for k, v in summary.items() if k != "alert"})
            return summary
    if item is not None and item.get("status") == "handled":
        summary.update(item_id=item["item_id"], status="approved already", posted=False)
        return summary
    if item is not None:
        if ctx.live and not (item.get("payload") or {}).get("posted"):
            sent = _post(ctx, item)
            summary.update(item_id=item["item_id"], status="posted", alert=sent)
        else:
            summary.update(item_id=item["item_id"], status="posted already" if (item.get("payload") or {}).get("posted")
                           else "recorded; a live run posts it")
        return summary
    payload = build(ctx, week, sample_size(ctx))
    if not has_work(payload) and not ctx.settings.general.auto_send:
        summary.update(skipped=True, reason=f"{AUTO_SEND_OFF}; no account is held for doubtful facts")
        log("hand_check_post", **summary)
        return summary
    if not has_work(payload):
        summary.update(status="nothing to check: no queued or verified account in an active industry group",
                       groups=active_groups(ctx.settings))
        log("hand_check_post", **summary)
        return summary
    item = _record(ctx, payload)
    sent = _post(ctx, item)
    summary.update(item_id=item["item_id"], status="recorded", accounts=len(payload["accounts"]),
                   doubtful=len(payload["doubtful"]), groups={g: len(ids) for g, ids in payload["groups"].items()},
                   alert=sent)
    log("hand_check_post", **{k: v for k, v in summary.items() if k != "alert"})
    return summary


def show(ctx: Context) -> tuple[dict | None, dict]:
    """(this week's item, or None when it is not recorded yet; the payload to print). It never writes: with no
    item (or one without the sample while auto_send is yes) it prints the draw `handcheck approve` records, the
    same all week since it is seeded with the week."""
    week = this_week(ctx)
    item = current(ctx, week)
    if item is not None and not needs_sample(ctx, item):
        return item, dict(item.get("payload") or {})
    return None, _with_sample(ctx, week, item)


def _resolve(payload: Mapping[str, Any], wanted: Iterable[str], known: set[str]) -> tuple[list[str], list[str]]:
    """(account ids for these ids or domains, the values matching no account)."""
    by_domain = {str(a.get("domain") or "").lower(): a["account_id"]
                 for a in [*(payload.get("accounts") or ()), *(payload.get("doubtful") or ())]}
    ids, unknown = [], []
    for w in wanted:
        v = str(w).strip()
        if not v:
            continue
        aid = by_domain.get(v.lower()) or (v if v in known else None)
        (ids if aid else unknown).append(aid or v)
    return list(dict.fromkeys(ids)), unknown


def approve(ctx: Context, pulled: Sequence[str], by: str) -> dict:
    """Mark this week's hand-check handled, with the pulled accounts. Live only; dry-run says what it would do.

    Every hand_check item of the week is marked (enrol waits for all of them); the pulled ids go
    on each, merged with any pulled before. A pull is an account id or a domain, in the sample or not.
    With no item yet (or one without the sample while auto_send is yes) it records the week's draw
    itself, the one `handcheck show` printed (seeded with the week), and approves that.
    """
    week = this_week(ctx)
    item = current(ctx, week)
    recorded = False
    if item is None or needs_sample(ctx, item):
        payload = _with_sample(ctx, week, item)
        if not has_work(payload):
            raise LookupError(f"nothing to check for {week}: no account is held for doubtful facts"
                              + ("" if not ctx.settings.general.auto_send
                                 else ", and no queued or verified account is in an active industry group"))
        item = _record(ctx, payload) if ctx.live else {"item_id": item_id(week), "kind": KIND, "status": "open",
                                                        "payload": payload}
        recorded = True
    payload = dict(item.get("payload") or {})
    sample = {a["account_id"] for a in [*(payload.get("accounts") or ()), *(payload.get("doubtful") or ())]}
    wanted = [str(p).strip() for p in pulled if str(p).strip()]
    on_file = {str(a["account_id"]) for a in ctx.store.select("accounts", {"account_id": wanted})} if wanted else set()
    ids, unknown = _resolve(payload, wanted, sample | on_file)
    if unknown:
        raise ValueError(f"no account with id or domain {', '.join(unknown)}")
    rows = [r for r in ctx.store.select("hitl_items", {"kind": KIND})
            if enrol._item_week(r) == week and r.get("item_id") != item["item_id"]] + [item]
    before = [x for r in rows for x in ((r.get("payload") or {}).get("pulled_account_ids") or ())]
    all_pulled = list(dict.fromkeys([*before, *ids]))
    cleared = [d for r in rows for d in ((r.get("payload") or {}).get("doubtful") or ())
               if d.get("account_id") not in all_pulled]
    missing = verify.missing_facts(ctx, [d["account_id"] for d in cleared])  # these stay on the check
    message = confirmation(ctx, week, by, all_pulled, len(cleared) - len(missing), len(missing))
    if ctx.live:
        ctx.store.upsert("hitl_items", [
            {"item_id": r["item_id"], "status": "handled", "handled_at": ctx.now, "handled_by": by,
             "payload": {**dict(r.get("payload") or {}), "pulled_account_ids": all_pulled}}
            for r in rows
        ])
        verify.clear_doubts(ctx, cleared, by, week)  # the next verify_accounts run decides on their facts
        if item.get("slack_ts"):
            notify.alert(ctx, message)
    return {"dry_run": ctx.dry_run, "iso_week": week, "item_id": item["item_id"], "approved": ctx.live,
            "was": "not recorded" if recorded else item.get("status"), "recorded": recorded and ctx.live,
            "pulled_account_ids": all_pulled,
            "doubts_cleared": [d["account_id"] for d in cleared if d["account_id"] not in missing] if ctx.live else [],
            "still_missing_a_fact": sorted(missing),
            "outside_sample": [i for i in ids if i not in sample], "checked": len(sample), "message": message}


def confirmation(ctx: Context, week: str, by: str, pulled: Sequence[str], cleared: int, missing: int = 0) -> str:
    """What approving says in the channel, in plain words: what happens next, in the mode auto_send is in.
    cleared: the accounts verify_accounts checks again; missing: those still missing a fact, which stay."""
    head = f"{week_label(week)} check approved by {by}" + (f"; pulled: {', '.join(pulled)}" if pulled else "")
    tomorrow = ctx.today_uk() + timedelta(days=1)
    when = "tomorrow" if tomorrow.weekday() < 5 else "on Monday"  # verify_accounts runs weekdays at 04:30 UK
    doubts = ("the account with doubtful facts is" if cleared == 1 else f"the {cleared} accounts with doubtful facts are")
    doubts = f"{doubts} verified again at 04:30 {when}" if cleared else ""
    stays = ("" if not missing else
             ("The account" if missing == 1 else f"The {missing} accounts") + " missing a fact (HQ state, size or "
             f"industry) {'stays' if missing == 1 else 'stay'} on the check until an Overrides row fills it in.")
    if not ctx.settings.general.auto_send:
        said = " ".join(x for x in ((f"{doubts[:1].upper()}{doubts[1:]}." if doubts else ""), stays) if x)
        return f"{head}. " + (said or "Nothing else waits on it.")
    return (f"{head}. New leads can go to Instantly this week" + (f"; {doubts}." if doubts else ".")
            + (f" {stays}" if stays else ""))
