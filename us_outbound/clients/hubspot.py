"""HubSpot CRM client (SPEC 1.2, 4, 11, 13): warm leads only.

Every call is an Op the guard judges. Writes are limited to the six us_outbound_*
properties, notes, tasks, Spill 3.0 deals and the three empty-only fields; records are
created only for positive or referral replies. Reads (search, pipelines, owners,
properties) are free in any mode. In dry-run every write returns its dry_result and
sends nothing.

API: CRM v3 objects and search, v4 default associations, v3 pipelines/owners/properties,
communication preferences v4 and the v3 GDPR delete.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, date, datetime
from typing import Any

from us_outbound.clients.guard import HUBSPOT_EMPTY_ONLY, HUBSPOT_PROPERTY_GROUP, Op
from us_outbound.clients.http import HttpClient

SEARCH_PAGE = 100  # HubSpot allows up to 200 per search page
SEARCH_CAP = 10_000  # search never pages past 10k results; we re-query on hs_object_id instead

COMPANY_PROPS = (
    "name",
    "domain",
    "lifecyclestage",
    "hs_lead_status",
    "hubspot_owner_id",
    "us_outbound_account_id",
    "us_outbound_tier",
    "us_outbound_industry_group",
    "us_outbound_top_signals",
)
CONTACT_PROPS = (
    "email",
    "firstname",
    "lastname",
    "jobtitle",
    "lifecyclestage",
    "hs_lead_status",
    "hubspot_owner_id",
    "hs_email_optout",
    "associatedcompanyid",
    "us_outbound_angle",
    "us_outbound_reply_class",
)
DEAL_PROPS = ("dealname", "pipeline", "dealstage", "hubspot_owner_id", "hs_is_closed", "closedate")

# HubSpot-defined association type ids, keyed (from, to).
# PHASE0-CONFIRM: ids from HubSpot's association type table; deal->company 5 is the
# "primary" label (341 is unlabeled) and contact->company 279 is unlabeled.
ASSOCIATION_TYPE_IDS: dict[tuple[str, str], int] = {
    ("notes", "companies"): 190,
    ("notes", "contacts"): 202,
    ("notes", "deals"): 214,
    ("tasks", "companies"): 192,
    ("tasks", "contacts"): 204,
    ("tasks", "deals"): 216,
    ("deals", "companies"): 5,
    ("contacts", "companies"): 279,
}

_PLURAL = {"company": "companies", "contact": "contacts", "deal": "deals", "note": "notes", "task": "tasks"}
_SINGULAR = {v: k for k, v in _PLURAL.items()}

_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _quote(text: str) -> str:
    """Percent-encode one URL path segment (an email in a path must not carry a raw '+')."""
    return "".join(chr(b) if b in _UNRESERVED else f"%{b:02X}" for b in text.encode())


def plural_type(name: str) -> str:
    """'company' or 'companies' -> 'companies'."""
    n = name.strip().lower()
    return _PLURAL.get(n, n)


def _timestamp(value: datetime | date) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
    else:
        dt = datetime(value.year, value.month, value.day, 17, 0, tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _associations(from_type: str, targets: Iterable[tuple[str, str]]) -> list[dict]:
    out = []
    for to_type, to_id in targets:
        key = (from_type, plural_type(to_type))
        if key not in ASSOCIATION_TYPE_IDS:
            raise ValueError(f"no HubSpot association type for {key[0]} -> {key[1]}")
        out.append(
            {
                "to": {"id": str(to_id)},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": ASSOCIATION_TYPE_IDS[key]}],
            }
        )
    return out


class HubSpot(HttpClient):
    system = "hubspot"
    base_url = "https://api.hubapi.com"

    # -- reads -----------------------------------------------------------------

    def _search(
        self,
        obj: str,
        filter_groups: list[dict],
        properties: Iterable[str],
        sorts: list[dict] | None = None,
        limit: int = SEARCH_PAGE,
    ) -> Iterator[dict]:
        """POST /crm/v3/objects/{obj}/search, following the after cursor."""
        after: str | None = None
        while True:
            body: dict[str, Any] = {"filterGroups": filter_groups, "properties": list(properties), "limit": limit}
            if sorts:
                body["sorts"] = sorts
            if after:
                body["after"] = after
            page = self.request(
                "POST", f"/crm/v3/objects/{obj}/search", Op(f"{_SINGULAR.get(obj, obj)}.search", target=obj), json=body
            ) or {}
            yield from page.get("results", [])
            after = ((page.get("paging") or {}).get("next") or {}).get("after")
            if not after or (str(after).isdigit() and int(after) >= SEARCH_CAP):
                return

    def _get(self, obj: str, record_id: str, properties: Iterable[str]) -> dict:
        body = self.request(
            "GET",
            f"/crm/v3/objects/{obj}/{record_id}",
            Op(f"{_SINGULAR.get(obj, obj)}.get", target=obj),
            params={"properties": ",".join(properties)},
        )
        return (body or {}).get("properties", {}) or {}

    def search_companies_by_domain(self, domain: str) -> list[dict]:
        """Companies whose primary domain is exactly this root domain (or its www. form)."""
        root = domain.strip().lower().removeprefix("www.")
        groups = [
            {"filters": [{"propertyName": "domain", "operator": "EQ", "value": d}]} for d in (root, f"www.{root}")
        ]
        return [{"id": r["id"], "properties": r.get("properties", {})} for r in self._search("companies", groups, COMPANY_PROPS)]

    def search_contacts_by_email(self, email: str) -> list[dict]:
        groups = [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email.strip().lower()}]}]
        return [{"id": r["id"], "properties": r.get("properties", {})} for r in self._search("contacts", groups, CONTACT_PROPS)]

    def opted_out_contacts_at_domain(self, domain: str, limit: int = 1) -> list[dict]:
        """Up to `limit` contacts on this email domain who opted out of all email or hard-bounced.

        One search page: the caller only needs to know whether there is one (SPEC 9 hard exclusions).
        PHASE0-CONFIRM: hs_email_domain, HubSpot's "Email domain" contact property, is searchable with EQ.
        """
        root = domain.strip().lower().removeprefix("www.")
        on_domain = {"propertyName": "hs_email_domain", "operator": "EQ", "value": root}
        groups = [
            {"filters": [on_domain, {"propertyName": "hs_email_optout", "operator": "EQ", "value": "true"}]},
            {"filters": [on_domain, {"propertyName": "hs_email_hard_bounce_reason_enum", "operator": "HAS_PROPERTY"}]},
        ]
        out: list[dict] = []
        for r in self._search("contacts", groups, CONTACT_PROPS, limit=limit):
            out.append({"id": r["id"], "properties": r.get("properties", {})})
            if len(out) >= limit:
                break
        return out

    def find_pipeline(self, label: str, object_type: str = "deals") -> tuple[str, str] | None:
        """(pipeline id, id of its first stage by displayOrder) for the pipeline with this label."""
        body = self.request("GET", f"/crm/v3/pipelines/{object_type}", Op("pipeline.list", target=object_type)) or {}
        want = label.strip().casefold()
        for p in body.get("results", []):
            if str(p.get("label", "")).strip().casefold() == want:
                stages = sorted(p.get("stages", []), key=lambda s: s.get("displayOrder", 0))
                return (str(p["id"]), str(stages[0]["id"])) if stages else None
        return None

    def owner_id_for_email(self, email: str) -> str | None:
        body = self.request(
            "GET", "/crm/v3/owners/", Op("owner.list", target="owners"), params={"email": email.strip().lower(), "limit": 100}
        ) or {}
        results = body.get("results", [])
        return str(results[0]["id"]) if results else None

    def open_deals_for_company(self, company_id: str) -> list[dict]:
        """Deals associated with the company that are not closed (an unknown state counts as open)."""
        # PHASE0-CONFIRM: search filters on the associations.company pseudo-property.
        groups = [{"filters": [{"propertyName": "associations.company", "operator": "EQ", "value": str(company_id)}]}]
        deals = [{"id": r["id"], "properties": r.get("properties", {})} for r in self._search("deals", groups, DEAL_PROPS)]
        return [d for d in deals if str(d["properties"].get("hs_is_closed", "")).lower() != "true"]

    def properties(self, object_type: str) -> list[dict]:
        body = self.request("GET", f"/crm/v3/properties/{object_type}", Op("property.list", target=object_type)) or {}
        return body.get("results", [])

    def property_groups(self, object_type: str) -> list[dict]:
        body = self.request(
            "GET", f"/crm/v3/properties/{object_type}/groups", Op("property_group.list", target=object_type)
        ) or {}
        return body.get("results", [])

    def iter_opted_out_or_bounced_emails(self) -> Iterator[str]:
        """Emails of contacts opted out of all email or hard-bounced, each yielded once.

        Pages on hs_object_id > last seen (sorted ascending) rather than the after cursor,
        so the search API's 10k-result cap never truncates the list.
        """
        seen: set[str] = set()
        for condition in (
            {"propertyName": "hs_email_optout", "operator": "EQ", "value": "true"},
            {"propertyName": "hs_email_hard_bounce_reason_enum", "operator": "HAS_PROPERTY"},
        ):
            last = "0"
            while True:
                groups = [{"filters": [condition, {"propertyName": "hs_object_id", "operator": "GT", "value": last}]}]
                body = self.request(
                    "POST",
                    "/crm/v3/objects/contacts/search",
                    Op("contact.search", target="contacts"),
                    json={
                        "filterGroups": groups,
                        "properties": ["email", "hs_object_id"],
                        "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
                        "limit": SEARCH_PAGE,
                    },
                ) or {}
                results = body.get("results", [])
                for r in results:
                    email = str((r.get("properties") or {}).get("email") or "").strip().lower()
                    if email and email not in seen:
                        seen.add(email)
                        yield email
                if len(results) < SEARCH_PAGE:
                    break
                last = str(results[-1]["id"])

    # -- writes (guarded; dry-run returns dry_result) ----------------------------

    def _current_for(self, obj: str, record_id: str, properties: Mapping[str, Any], current: Mapping[str, Any] | None) -> dict:
        """Existing values of the empty-only fields being set, read fresh from HubSpot just before the PATCH.

        A caller's "empty" is never trusted (a search result can be stale, and someone may
        have set the owner since). A caller's non-empty value is kept: it can only make the
        guard refuse, so no read is needed.
        """
        wanted = sorted(set(properties) & HUBSPOT_EMPTY_ONLY)
        if not wanted:
            return {}
        caller = dict(current or {})
        if any(caller.get(k) not in (None, "") for k in wanted):
            return {k: caller.get(k) for k in wanted}
        fresh = self._get(obj, record_id, wanted)
        return {k: fresh.get(k) for k in wanted}

    def _create(self, obj: str, properties: Mapping[str, Any], reply_class: str) -> dict | None:
        op = Op(
            f"{_SINGULAR[obj]}.create",
            target=obj,
            write=True,
            detail={"properties": sorted(properties), "reply_class": reply_class},
        )
        return self.request(
            "POST",
            f"/crm/v3/objects/{obj}",
            op,
            json={"properties": dict(properties)},
            dry_result={"id": None, "dry_run": True, "properties": dict(properties)},
        )

    def _update(self, obj: str, record_id: str, properties: Mapping[str, Any], current: Mapping[str, Any] | None) -> dict | None:
        op = Op(
            f"{_SINGULAR[obj]}.update",
            target=obj,
            write=True,
            detail={
                "id": str(record_id),
                "properties": sorted(properties),
                "current": self._current_for(obj, record_id, properties, current),
            },
        )
        return self.request(
            "PATCH",
            f"/crm/v3/objects/{obj}/{record_id}",
            op,
            json={"properties": dict(properties)},
            dry_result={"id": str(record_id), "dry_run": True, "properties": dict(properties)},
        )

    def create_company(self, properties: Mapping[str, Any], *, reply_class: str) -> dict | None:
        return self._create("companies", properties, reply_class)

    def update_company(self, company_id: str, properties: Mapping[str, Any], *, current: Mapping[str, Any] | None) -> dict | None:
        return self._update("companies", company_id, properties, current)

    def create_contact(self, properties: Mapping[str, Any], *, reply_class: str) -> dict | None:
        return self._create("contacts", properties, reply_class)

    def update_contact(self, contact_id: str, properties: Mapping[str, Any], *, current: Mapping[str, Any] | None) -> dict | None:
        return self._update("contacts", contact_id, properties, current)

    def associate(self, from_type: str, from_id: str, to_type: str, to_id: str) -> None:
        """Default (unlabeled) association through the v4 API."""
        f, t = plural_type(from_type), plural_type(to_type)
        self.request(
            "PUT",
            f"/crm/v4/objects/{f}/{from_id}/associations/default/{t}/{to_id}",
            Op("association.create", target=f"{f}/{t}", write=True, detail={"from_id": str(from_id), "to_id": str(to_id)}),
        )

    def create_note(self, body: str, associations: list[tuple[str, str]], *, at: datetime | None = None) -> dict | None:
        """A note (hs_note_body is rendered as HTML by HubSpot) on the given (object type, id) records."""
        payload = {
            "properties": {"hs_note_body": body, "hs_timestamp": _timestamp(at or datetime.now(UTC))},
            "associations": _associations("notes", associations),
        }
        op = Op("note.create", target="notes", write=True, detail={"associations": [f"{t}:{i}" for t, i in associations]})
        return self.request("POST", "/crm/v3/objects/notes", op, json=payload, dry_result={"id": None, "dry_run": True})

    def create_task(
        self, subject: str, body: str, owner_id: str, due: datetime | date, associations: list[tuple[str, str]]
    ) -> dict | None:
        """A to-do for owner_id due at `due`; HubSpot emails the owner its task notification (SPEC 11)."""
        payload = {
            "properties": {
                "hs_task_subject": subject,
                "hs_task_body": body,
                "hubspot_owner_id": str(owner_id),
                "hs_timestamp": _timestamp(due),
                "hs_task_status": "NOT_STARTED",
                "hs_task_priority": "HIGH",
                "hs_task_type": "TODO",
            },
            "associations": _associations("tasks", associations),
        }
        op = Op(
            "task.create",
            target="tasks",
            write=True,
            detail={"owner_id": str(owner_id), "associations": [f"{t}:{i}" for t, i in associations]},
        )
        return self.request("POST", "/crm/v3/objects/tasks", op, json=payload, dry_result={"id": None, "dry_run": True})

    def create_deal(self, dealname: str, pipeline: str, dealstage: str, owner_id: str, company_id: str) -> dict | None:
        properties = {"dealname": dealname, "pipeline": pipeline, "dealstage": dealstage, "hubspot_owner_id": str(owner_id)}
        op = Op(
            "deal.create",
            target="deals",
            write=True,
            detail={
                "properties": sorted(properties),
                "dealname": dealname,
                "pipeline": pipeline,
                "dealstage": dealstage,
                "company_id": str(company_id),
            },
        )
        payload = {"properties": properties, "associations": _associations("deals", [("companies", company_id)])}
        return self.request("POST", "/crm/v3/objects/deals", op, json=payload, dry_result={"id": None, "dry_run": True})

    def ensure_property_group(self, object_type: str) -> None:
        """Create the "US Outbound" property group on this object type if it is missing."""
        if any(g.get("name") == HUBSPOT_PROPERTY_GROUP for g in self.property_groups(object_type)):
            return
        self.request(
            "POST",
            f"/crm/v3/properties/{object_type}/groups",
            Op("property_group.create", target=HUBSPOT_PROPERTY_GROUP, write=True, detail={"object_type": object_type}),
            json={"name": HUBSPOT_PROPERTY_GROUP, "label": "US Outbound", "displayOrder": -1},
        )

    def create_property(self, object_type: str, spec: Mapping[str, Any]) -> None:
        """Post a property spec as given; the guard allows only the six us_outbound_* names."""
        self.request(
            "POST",
            f"/crm/v3/properties/{object_type}",
            Op(
                "property.create",
                target=str(spec.get("name", "")),
                write=True,
                detail={"object_type": object_type, "group": spec.get("groupName")},
            ),
            json=dict(spec),
        )

    def unsubscribe(self, email: str) -> None:
        """Opt the address out of every email subscription (communication preferences v4)."""
        email = email.strip().lower()
        self.request(
            "POST",
            f"/communication-preferences/v4/statuses/{_quote(email)}/unsubscribe-all",
            Op("communication.unsubscribe", target="contacts", write=True, detail={"email": email}),
            params={"channel": "EMAIL"},
        )

    def gdpr_delete_contact(self, email: str) -> None:
        """Permanently delete the contact with this email. Called only by the erase command (SPEC 6)."""
        email = email.strip().lower()
        self.request(
            "POST",
            "/crm/v3/objects/contacts/gdpr-delete",
            Op("contact.gdpr_delete", target="contacts", write=True, detail={"email": email, "erasure_request": True}),
            json={"objectId": email, "idProperty": "email"},
        )
