"""The ICP gate (us_outbound/icp.py; W2-2, 20 Oct 2026): every rule that keeps a company out, written once.

One case per reason code, the plan's cases, how each stage reads a gap, the gate version, and parity with what
verify.check, enrol.account_block and tiers.hard_exclusion said of 200 generated accounts before the change
(tests/fixtures/icp_parity.json, recorded at wave2 379ba97), with every disagreement named and explained.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from us_outbound import icp, verify
from us_outbound.enrol import enrol, queue
from us_outbound.scoring import tiers
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import General
from us_outbound.settings.validate import validate_all

S, _ = validate_all(default_tabs())
TODAY = date(2026, 10, 27)
ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "icp_parity.json"
OFF = next(i.industry for i in S.industries if not i.active)  # "Remote & hybrid teams" on the default sheet


def clean(**kw) -> dict:
    """A clean 64-staff NY tech account: it passes every rule."""
    return {"account_id": "c1", "domain": "clean.com", "source": "apollo", "hq_state": "NY",
            "hq_country": "United States", "employees": 64, "size_band": "50-99", "industry": "Fintech",
            "naics": "541511", "apollo_industry": "Software", "founded_year": 2010, **kw}


def gate(account: dict, facts: dict | None = None, *, checks=icp.ALL, **kw) -> icp.Reason | None:
    return icp.gate(account, facts or {}, S, today=TODAY, checks=checks, **kw)


# -- one case per code ------------------------------------------------------------------------------------------------

CASES = [
    # (code, account changes, facts, gate keywords)
    ("incomplete", {"domain": ""}, {}, {}),
    ("suppressed", {}, {}, {"suppressed": {"clean.com"}}),
    ("public_body", {"domain": "town.gov"}, {}, {}),
    ("partner", {}, {}, {"partners": {"clean.com"}}),
    ("held", {}, {}, {"held": {"c1"}}),
    ("state_unknown", {"hq_state": None}, {}, {}),
    ("state", {"hq_state": "CA"}, {}, {}),
    ("size", {"employees": 250}, {}, {}),
    ("no_label", {"industry": "Not a label"}, {}, {}),
    ("industry_off", {"industry": OFF}, {}, {}),
    ("hubspot_customer", {}, {"hubspot_former_customer": True}, {}),
    ("hubspot_open_deal", {}, {"hubspot_open_deal": True}, {}),
    ("hubspot_owner", {}, {"hubspot_other_owner": True}, {}),
    ("hubspot_opted_out", {}, {"hubspot_opted_out_or_bounced": True}, {}),
    ("declined", {}, {icp.DECLINED_IN_SLACK: True}, {}),
    ("founded", {"founded_year": 2025}, {}, {}),  # 1 Jan 2027 is the earliest it is 2 years old; today 27 Oct 2026
]


@pytest.mark.parametrize(("code", "changes", "facts", "kw"), CASES, ids=[c[0] for c in CASES])
def test_one_case_per_reason_code(code, changes, facts, kw):
    assert gate(clean(**changes), facts, **kw).code == code


def test_the_cases_cover_every_code_and_verify_files_each_one():
    assert {c[0] for c in CASES} == icp.CODES
    assert icp.CODES <= set(verify.REASON_CODES)  # every code the gate returns says what verify does with it


def test_the_plan_s_cases():
    assert gate(clean()) is None  # a clean 64-staff NY tech account
    visitor = clean(hq_state="CA", source=icp.VISITOR_SOURCE)
    assert icp.any_us_state(visitor) and gate(visitor) is None  # a CA website visitor: any US state will do
    assert gate(clean(hq_state="CA")) == ("state", "HQ in CA, which is never contacted")  # a CA HQ, not a visitor
    # The General range is 10 to 249 on the default sheet: 9 and 250 are out, 10 and 249 in.
    assert gate(clean(employees=9)) == ("size", "outside 10 to 249 employees")
    assert gate(clean(employees=250)).code == "size"
    assert gate(clean(employees=10)) is None and gate(clean(employees=249)) is None
    # With no count, the size band: 250-499 is outside, 10-19 inside.
    assert gate(clean(employees=None, size_band="250-499")).code == "size"
    assert gate(clean(employees=None, size_band="10-19")) is None


def test_the_first_failing_rule_in_order_is_the_reason():
    """verify's order, so the code it writes is what it was: a CA company with an open deal is out for good for its
    state, not for 30 days for the deal."""
    assert gate(clean(hq_state="CA"), {"hubspot_open_deal": True}).code == "state"
    assert gate(clean(employees=300, industry=OFF)).code == "size"


def test_each_stage_reads_a_gap_as_it_did():
    """complete (verify's stage): a missing size is incomplete, a missing label no_label, and the HQ state must be on
    the row; without it (the send checks, the rescore) a gap passes and a state fact stands in for a blank column."""
    no_size = clean(employees=None, size_band=None)
    assert gate(no_size, checks=icp.VERIFY).code == "incomplete"
    assert gate(no_size, checks=icp.SEND) is None
    no_label = clean(industry="")
    assert gate(no_label, checks=icp.VERIFY).code == "no_label"
    assert gate(no_label, checks=icp.SEND) is None
    blank_state = clean(hq_state="")
    assert gate(blank_state, {"hq_state": "NY"}, checks=icp.VERIFY).code == "state_unknown"
    assert gate(blank_state, {"hq_state": "NY"}, checks=icp.SCORE) is None
    assert gate(blank_state, {"hq_state": "CA"}, checks=icp.SCORE).code == "state"
    # The rescore's checks never look at size, label, domain or the hand-check; the send checks never at state or
    # HubSpot facts (they are tiered Excluded by the rescore, which enrol reads).
    assert gate(clean(employees=900, industry=OFF, domain=""), checks=icp.SCORE, held={"c1"}) is None
    assert gate(clean(hq_state="CA"), {"hubspot_customer": True}, checks=icp.SEND) is None


def test_the_callers_go_through_the_gate():
    """verify.check, enrol.account_block and tiers.hard_exclusion are the gate's three stages."""
    held = clean(hq_state="TX")
    assert verify.check(clean(employees=300), {}, S, set(), set()) == ("size", "outside 10 to 249 employees")
    assert enrol.account_block(held, S, set(), set(), {"c1"}) == icp.HELD_TEXT == enrol.HAND_CHECK_SKIP
    assert tiers.hard_exclusion(clean(), {"hubspot_customer": True}, S, TODAY) == "a customer in HubSpot"


# -- the gate version -------------------------------------------------------------------------------------------------


def test_the_rules_hash_names_the_gate_s_version():
    """12 hex characters; the same for the same rules; another with another size range or another active state (the
    sheet parts the gate reads beside the labels, which labels_hash covers)."""
    h = icp.rules_hash(S)
    assert re.fullmatch(r"[0-9a-f]{12}", h) and h == icp.rules_hash(S)
    narrower = S.__class__(**{**S.__dict__, "general": General(**{**S.general.__dict__, "max_employees": 199})})
    assert icp.rules_hash(narrower) != h
    fewer = S.__class__(**{**S.__dict__, "states": tuple(st for st in S.states if st.state != "GA")})
    assert icp.rules_hash(fewer) != h
    assert icp.rules_hash() != h  # the code's rules alone


# -- one definition ---------------------------------------------------------------------------------------------------


def test_never_states_and_the_retired_tier_keys_are_defined_once_or_not_at_all():
    """The plan's acceptance: one NEVER_STATES; no code path reads priority_threshold, standard_threshold,
    auto_thresholds or queue.CONTROL."""
    code = {p: p.read_text() for p in (ROOT / "us_outbound").rglob("*.py")}
    defined = [p.relative_to(ROOT).as_posix() for p, t in code.items() if re.search(r"^NEVER_STATES\s*=", t, re.M)]
    assert defined == ["us_outbound/icp.py"]
    reads = [p.relative_to(ROOT).as_posix() for p, t in code.items()  # comments aside
             if re.search(r"\.(priority_threshold|standard_threshold|auto_thresholds|control_share)\b|queue\.CONTROL",
                          re.sub(r"#.*", "", t))]
    assert reads == []
    assert not {"priority_threshold", "standard_threshold", "auto_thresholds", "control_share"} & set(
        General.__dataclass_fields__)
    assert not hasattr(queue, "CONTROL") and not hasattr(queue, "TIER_RANK")
    assert not (ROOT / "us_outbound" / "scoring" / "balance.py").exists()


# -- parity with the gate before W2-2 ---------------------------------------------------------------------------------

# The reviewed drift (W2-2 "Live safety": no account that passes today fails tomorrow unless a drift is shown and
# accepted). Hand-counted from the 200 recorded cases:
#   dead rules (code-scoring B5; no source ever wrote their facts): us_headcount 3 (co003, co095, co113, co131, co178,
#     co181), ca_wa_share 0.3 (co028, co086, co125, co127), ca_wa_share 0.1 + fl_share 0.15 (co027, co141, co159):
#     13 accounts the rescore excluded and now does not; verify never read these facts, so it verified them anyway;
#   a state written out: "New York" (co016, co050, co068, co117, co147, co185): the rescore read it upper-cased, not
#     as its code, and excluded it as "HQ state NEW YORK is not active", while verify read NY and verified it: 6;
#   a partner by NAICS or keywords on the account row (co115, co133, co156, co188): enrol's send checks looked only at
#     the partners table; now they also match the row, a stricter send check for accounts verify and the rescore
#     had already kept out (asserted below): 4.
DEAD_RULES = {"co003", "co095", "co113", "co131", "co178", "co181", "co028", "co086", "co125", "co127", "co027",
              "co141", "co159"}
WRITTEN_OUT_STATE = {"co016", "co050", "co068", "co117", "co147", "co185"}
PARTNER_ON_THE_ROW = {"co115", "co133", "co156", "co188"}


def test_the_gate_agrees_with_the_recorded_checks_but_for_the_reviewed_drift():
    data = json.loads(FIXTURE.read_text())
    assert data["recorded_at"].startswith("wave2 379ba97") and len(data["cases"]) == 200
    today = date.fromisoformat(data["today"])
    verify_off, send_off, score_off = [], [], []
    for c in data["cases"]:
        a, facts = c["account"], c["facts"]
        domain = (a["domain"] or "").lower()
        suppressed = {domain} if c["suppressed"] and domain else set()
        partners = {domain} if c["partner"] and domain else set()
        held = {a["account_id"]} if c["pulled"] else set()
        v = icp.gate(a, facts, S, suppressed=suppressed, partners=partners, checks=icp.VERIFY)
        b = icp.gate(a, {}, S, suppressed=suppressed, partners=partners, held=held, checks=icp.SEND)
        h = icp.gate(a, facts, S, today=today, checks=icp.SCORE)
        if (c["verify_check"] is None) != (v is None) or (v and v.code != c["verify_check"][0]):
            verify_off.append(a["account_id"])
        if (c["account_block"] is None) != (b is None):
            send_off.append((a["account_id"], c, b))
        if (c["hard_exclusion"] is None) != (h is None):
            score_off.append((a["account_id"], c, h))
    # verify: the same answer and the same code for all 200.
    assert verify_off == []
    # The rescore: only the dead rules and the written-out state, each passing now where it was excluded.
    assert {aid for aid, _, _ in score_off} == DEAD_RULES | WRITTEN_OUT_STATE
    for aid, c, now in score_off:
        assert now is None
        if aid in WRITTEN_OUT_STATE:
            assert c["account"]["hq_state"] == "New York" and c["hard_exclusion"] == "HQ state NEW YORK is not active"
        else:
            assert re.search(r"US-located people|of US staff are in", c["hard_exclusion"])
    # enrol's send checks: only partners by the row, each already out at verify and at the rescore.
    assert {aid for aid, _, _ in send_off} == PARTNER_ON_THE_ROW
    for _, c, now in send_off:
        assert now.code == "partner" and c["account_block"] is None
        assert c["verify_check"][0] == "partner" and c["hard_exclusion"] is not None
