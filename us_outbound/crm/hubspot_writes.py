"""HubSpot writes (SPEC 1.2, 11). Phase 0 part: the six us_outbound_* properties and the ids to look up.

ensure_properties() creates the property group "US Outbound" (internal name us_outbound)
on companies and contacts, then any of the six properties that is missing. It never
changes an existing property: if one of the six names already exists with a different
type, it reports the clash and creates nothing at all. Dry-run creates nothing and says
what it would create.

lookup_pipeline() reads the "Spill 3.0" pipeline, its first stage and Harry's owner id,
for Harry to paste into the General tab. It never writes settings itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from us_outbound.clients.guard import HUBSPOT_PROPERTY_GROUP, HUBSPOT_SIX_PROPS
from us_outbound.context import Context
from us_outbound.logs import log
from us_outbound.settings.model import TIERS

GROUP_LABEL = "US Outbound"
# SPEC 11 classification classes, in plain English for the dropdown.
REPLY_CLASSES: dict[str, str] = {
    "positive": "Positive",
    "referral": "Referral",
    "objection": "Objection",
    "not_now": "Not now",
    "negative": "Negative",
    "out_of_office": "Out of office",
    "wrong_person": "Wrong person",
    "unsubscribe": "Unsubscribe",
    "other": "Other",
}


def _options(values: Mapping[str, str]) -> list[dict]:
    return [{"label": label, "value": value, "displayOrder": i, "hidden": False} for i, (value, label) in enumerate(values.items())]


def _spec(name: str, label: str, description: str, type_: str, field_type: str, **extra: Any) -> dict:
    return {
        "name": name,
        "label": label,
        "description": description,
        "groupName": HUBSPOT_PROPERTY_GROUP,
        "type": type_,
        "fieldType": field_type,
        "formField": False,
        **extra,
    }


# (object type, property spec) for the six properties of SPEC 11, in the group "US Outbound".
PROPERTY_SPECS: tuple[tuple[str, dict], ...] = (
    (
        "companies",
        _spec(
            "us_outbound_account_id", "US Outbound account id",
            "The account's id in the US Outbound system (BigQuery us_outbound.accounts). Set by the jobs.",
            "string", "text", hasUniqueValue=True,
        ),
    ),
    (
        "companies",
        _spec(
            "us_outbound_tier", "US Outbound tier", "The account's tier when it replied. Set by the jobs.",
            "enumeration", "select", options=_options({t: t for t in TIERS}),
        ),
    ),
    (
        "companies",
        _spec(
            "us_outbound_industry_group", "US Outbound industry group",
            "The website industry group the account was found under. Set by the jobs.", "string", "text",
        ),
    ),
    (
        "companies",
        _spec(
            "us_outbound_top_signals", "US Outbound top signals",
            "The signals that scored the account, with their evidence. Set by the jobs.", "string", "textarea",
        ),
    ),
    (
        "contacts",
        _spec(
            "us_outbound_angle", "US Outbound angle",
            "The angle and copy version this contact was sent. Set by the jobs.", "string", "text",
        ),
    ),
    (
        "contacts",
        _spec(
            "us_outbound_reply_class", "US Outbound reply class",
            "How the contact's reply was classified. Set by the jobs.", "enumeration", "select",
            options=_options(REPLY_CLASSES),
        ),
    ),
)
assert {s["name"] for _, s in PROPERTY_SPECS} == HUBSPOT_SIX_PROPS
OBJECT_TYPES = ("companies", "contacts")


class PropertyClash(Exception):
    """One of the six names already exists on HubSpot with a different type. Nothing was created."""


def ensure_properties(ctx: Context) -> dict:
    """Create the group and any missing us_outbound_* property; never modify an existing one."""
    hs = ctx.clients.hubspot
    existing = {obj: {p.get("name"): p for p in hs.properties(obj)} for obj in OBJECT_TYPES}
    clashes: list[str] = []
    missing: list[tuple[str, dict]] = []
    present: list[str] = []
    notes: list[str] = []
    for obj, spec in PROPERTY_SPECS:
        have = existing[obj].get(spec["name"])
        if have is None:
            missing.append((obj, spec))
            continue
        present.append(spec["name"])
        if have.get("type") != spec["type"]:
            clashes.append(f"{obj} {spec['name']}: exists as type {have.get('type')!r}, we need {spec['type']!r}")
            continue
        if have.get("groupName") != HUBSPOT_PROPERTY_GROUP:
            notes.append(f"{obj} {spec['name']} exists in group {have.get('groupName')!r}; left as it is")
        if spec["type"] == "enumeration":
            want = {o["value"] for o in spec["options"]}
            got = {o.get("value") for o in have.get("options") or []}
            if want - got:
                notes.append(f"{obj} {spec['name']} lacks options {sorted(want - got)}; left as it is (add them by hand)")
    if clashes:
        log("hubspot_setup_clash", clashes=clashes)
        raise PropertyClash("; ".join(clashes) + ". Nothing was created; ask Harry which property to use.")

    for obj in OBJECT_TYPES:
        hs.ensure_property_group(obj)
    for obj, spec in missing:
        hs.create_property(obj, spec)
    names = [s["name"] for _, s in missing]
    summary = {
        "dry_run": ctx.dry_run,
        "group": HUBSPOT_PROPERTY_GROUP,
        ("would_create" if ctx.dry_run else "created"): names,
        "existing": present,
        "notes": notes,
    }
    log("hubspot_setup", **summary)
    return summary


def lookup_pipeline(ctx: Context) -> dict:
    """Ids for the General tab: the "Spill 3.0" pipeline, its first stage and Harry's owner id (reads only)."""
    g = ctx.settings.general
    hs = ctx.clients.hubspot
    found = hs.find_pipeline(g.hubspot_pipeline)
    owner = hs.owner_id_for_email(g.escalation_email)
    ids = {
        "hubspot_pipeline_id": found[0] if found else None,
        "hubspot_deal_stage_id": found[1] if found else None,
        "hubspot_owner_id": owner,
    }
    log("hubspot_lookup", pipeline=g.hubspot_pipeline, **ids)
    return ids


# -- Phase 2: reply writes (SPEC 11 "HubSpot writes", positive or referral replies only) --
# Find or create the company by root domain (hold and flag a manual merge on more than one),
# find or create the contact and associate it, add the note and Harry's task, set the three
# empty-only fields, and create the deal when a demo is requested or booked. Built in phase 2.
