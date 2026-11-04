"""Minimal HubSpot CRM client (standard library only) used by the review-request flow.

Needs a HubSpot *private app* access token in HUBSPOT_TOKEN with scopes:
  crm.objects.contacts.read, crm.objects.contacts.write, crm.objects.deals.read,
  crm.schemas.contacts.write   (only for creating the custom properties once)
"""
import json, os, time, urllib.error, urllib.request

API = "https://api.hubapi.com"

# association type ids (HUBSPOT_DEFINED)
NOTE_TO_CONTACT, NOTE_TO_DEAL, TASK_TO_CONTACT, TASK_TO_DEAL = 202, 214, 204, 216

PROPERTIES = [
    {"name": "cia_review_rating", "label": "CIA review rating", "type": "number", "fieldType": "number",
     "description": "1-5 star rating from the post-signing 'How did we do?' email"},
    {"name": "cia_review_comment", "label": "CIA review comment", "type": "string", "fieldType": "textarea",
     "description": "Private comment left with the satisfaction rating"},
    {"name": "cia_review_date", "label": "CIA review date", "type": "date", "fieldType": "date",
     "description": "When the client rated us"},
    {"name": "cia_review_google_clicked", "label": "CIA review: clicked Google", "type": "enumeration",
     "fieldType": "booleancheckbox", "options": [{"label": "Yes", "value": "true"}, {"label": "No", "value": "false"}],
     "description": "Client clicked through to leave a Google review"},
]


class HubSpotError(Exception):
    pass


def token():
    return os.environ.get("HUBSPOT_TOKEN", "")


def enabled():
    return bool(token())


def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise HubSpotError(f"{method} {path} -> {e.code}: {e.read()[:300].decode('utf-8', 'replace')}")


def ensure_properties():
    for p in PROPERTIES:
        try:
            call("POST", "/crm/v3/properties/contacts", {**p, "groupName": "contactinformation"})
            print("hubspot: created property", p["name"], flush=True)
        except HubSpotError as e:
            if "409" not in str(e):
                raise


def get_contact(cid):
    return call("GET", f"/crm/v3/objects/contacts/{int(cid)}?properties=email,firstname,lastname,company,hubspot_owner_id")


def verify_contact(cid, email):
    """The email link carries both the contact id and email; both must match so ratings can't be forged by guessing ids."""
    c = get_contact(cid)
    actual = (c.get("properties", {}).get("email") or "").strip().lower()
    if not actual or actual != (email or "").strip().lower():
        raise HubSpotError("contact/email mismatch")
    return c


def latest_deal(cid):
    """Most recently closed deal associated with the contact (best effort)."""
    res = call("GET", f"/crm/v4/objects/contacts/{int(cid)}/associations/deals")
    ids = [str(r["toObjectId"]) for r in res.get("results", [])]
    if not ids:
        return None
    deals = call("POST", "/crm/v3/objects/deals/batch/read",
                 {"properties": ["dealname", "closedate", "dealstage", "hubspot_owner_id"], "inputs": [{"id": i} for i in ids]})
    rows = sorted(deals.get("results", []), key=lambda d: d["properties"].get("closedate") or "", reverse=True)
    return rows[0] if rows else None


def update_contact(cid, props):
    call("PATCH", f"/crm/v3/objects/contacts/{int(cid)}", {"properties": props})


def _assoc(cid, deal_id, contact_type, deal_type):
    a = [{"to": {"id": str(cid)}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": contact_type}]}]
    if deal_id:
        a.append({"to": {"id": str(deal_id)}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": deal_type}]})
    return a


def add_note(cid, deal_id, html_body):
    call("POST", "/crm/v3/objects/notes", {
        "properties": {"hs_timestamp": int(time.time() * 1000), "hs_note_body": html_body},
        "associations": _assoc(cid, deal_id, NOTE_TO_CONTACT, NOTE_TO_DEAL)})


def add_task(cid, deal_id, owner_id, subject, body):
    props = {"hs_timestamp": int((time.time() + 86400) * 1000), "hs_task_subject": subject, "hs_task_body": body,
             "hs_task_status": "NOT_STARTED", "hs_task_priority": "HIGH", "hs_task_type": "CALL"}
    if owner_id:
        props["hubspot_owner_id"] = owner_id
    call("POST", "/crm/v3/objects/tasks", {"properties": props,
                                           "associations": _assoc(cid, deal_id, TASK_TO_CONTACT, TASK_TO_DEAL)})
