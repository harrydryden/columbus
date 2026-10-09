"""Send approvals: every email waits for an approver's ✅ in Slack before it reaches Instantly (Harry, 2 Oct 2026).

Harry, 2 Oct 2026: "every single message that gets sent out comes to this channel first for
approval", showing the company with a link to its domain, the recipient linked to where we found
them, the sender, the subject, the body and the number of emails sent. A tick sends it; a cross
offers to edit the email (and approve it again), to drop the contact (and find another) or to drop
the company. "There should be a switch to turn on and off auto send": the General key auto_send.
While it is no (the default) the enrol job proposes instead of enrolling; while it is yes, enrol adds
leads straight away, as before. live_sending stays the master switch: nothing reaches Instantly while
it is no.

Proposing (enrol, weekdays 12:00 UK; thread.propose). Each account enrol prepares becomes one item:
a hitl_items row and a card in the alert channel (#us-outbound). Approval is per contact and covers
the whole four-email sequence: the card shows email 1 in full and its first thread reply shows
emails 2 to 4, so the exact text of every message is seen before anything is sent; once approved,
Instantly sends email 1 in the next send window and the follow-ups on days 7, 14 and 21 by itself.
The bot seeds ✅ and ❌ on the card (Slack.react), so approving is one click; its own reactions never
count. Under the people, the card's Industry line (Harry, 7 Oct 2026; labels.py) gives the label, whether the rules
and the task model agreed, and which copy the emails are ("rules and model agree · Fintech copy", "⚠️ … · General
copy", "not checked by the model"); its They do line gives what the model read the company does, with its quote.
The facts line names the running copy test and the contact's arm ("Test: warm-intro · warm intro"), or says the
account is not in it and why (Harry, 7 Oct 2026; enrol/variants.py); the emails shown are the arm's. An edit keeps
the arm: ✅ records it on the contact, and the item's outcome approved_edited says it was edited (learn/looks.py
counts it in its arm, and says how many were).
Waiting items hold their sender's slots today and their place in the week (limits.today), and
an account with one waiting is not proposed again. With auto_send = no the weekly hand-check is not
a gate (every email is approved anyway): hand_check_post records only the accounts verify_accounts
held for doubtful facts, and golive's Hand-check line passes. A second contact at an account (General
second_contact; enrol/second.py; Harry, 6 Oct 2026) waits for its own ✅ like any email: its card is headed
"Send approval · second contact" and says who the first contact was and when their email 1 went; 🚫 on it
stops any further contact with the company, not the first person's emails.

Approving (poll_approvals, every 5 minutes, after the reply desk; polling.poll). Only an id on
approver_slack_ids counts; anyone else, and the bot, is ignored. Thread replies are read in order,
then the reactions on the message whose ✅ counts now (the card, or the latest edited version):
  ✅, or "send"                  re-check, then add the one lead to "US Outbound – {owner}" and record
                                 the enrollment as enrol does (enrol._record_enrolled). The card says
                                 "✅ Approved by @Harry at 14:02 UK · added to US Outbound – Hannah
                                 Spalding" and the thread confirms. A compare-and-set "sending" status
                                 means it is never added twice; if Instantly fails, the thread says why
                                 and the item waits for a fresh ✅ on that note (or "send"). A lead
                                 Instantly leaves out of its add summary is looked up in the campaign
                                 (enrol.campaign_lead_ids): there, it is recorded and approved; not
                                 there, Instantly refused it (its blocklist, or a lead in another
                                 campaign), so the card closes blocked and the contact is marked
                                 suppressed, and pick_contacts finds the next person. If a run dies
                                 mid-add (or the lookup fails), a later run looks it up: there, it is
                                 approved; not there, a person approves it again; Instantly unreadable,
                                 the thread asks a person to check the campaign first. A lead left out
                                 because Instantly's plan is full is no refusal (enrol/plan.py; Harry,
                                 7 Oct 2026): the card is held, the contact kept, the approvers asked
                                 once a day to make room, and the rest of that run's ✅s held too.
                                 The re-check (recheck) tells holds from blocks. A hold is temporary:
                                 live_sending or optout_tested no, an operator stop, the stop rule, the
                                 reply pause, a blackout date on Instantly's next send day, the owner's
                                 campaign not active in Instantly, Instantly or HubSpot not answering.
                                 The card stays open, the thread gets one note per hold reason, and the
                                 ✅ stays valid: each run tries again and the lead is added once the
                                 holds clear (or the card expires). Approving at the weekend is fine:
                                 Instantly sends on its next weekday. A block is permanent: the account
                                 or contact suppressed, excluded or no longer verified, the enrol run's
                                 own eligibility check (kill rules and hand-check pulls included), the
                                 email changed, no Active mailbox for the owner, another sender, or
                                 HubSpot excluding it now (the account is excluded too). The item
                                 closes as "blocked" with the reason.
  ❌, or "skip" / "no"           state "rejected": the thread offers three choices, their reactions seeded:
    ✏️ (or 📝), or "edit"        how to edit: reply in the thread with the new email 1, an optional first
                                 line "Subject: …" and then the body; "Email 2:" (3, 4) first changes a
                                 follow-up. Each edit is rendered by the renderer that renders for sending
                                 (render.render_step: HTML or text, the signature,
                                 every copy rule; the QA hash is left out, since the approver's ✅ is the
                                 review), and one that breaks a rule is refused in the thread with the
                                 rules it breaks. A passing edit is posted as the new version with ✅ and
                                 ❌ seeded: the re-approval. ✅ on it sends the edited sequence (outcome
                                 approved_edited); ❌ offers the three choices again. The first wording is
                                 kept in payload.original. Slack cannot open an editor without an
                                 interactive endpoint, which this app deliberately has none of (SPEC 2:
                                 no public endpoint), so edits are thread replies.
    👤, or "contact"             not this person: the contact is marked suppressed ("declined in Slack
                                 by …"; not the suppression table, as they did not opt out), so
                                 pick_contacts finds the next-ranked person at 05:30 (it skips people
                                 already revealed for the account) and a later enrol proposes them. The
                                 account stays verified (outcome contact_rejected).
    🚫, or "company"             drop the company: excluded with a declined_in_slack fact, which
                                 scoring/tiers.py reads as a hard exclusion, so a rescore keeps it out
                                 (enrol.mark_excluded; outcome company_rejected).
  When more than one choice is on the choices message, the least drastic wins (✏️, then 👤, then 🚫).
  "industry: Fintech"            in any state (Harry, 7 Oct 2026; labels.py): the company is that label, not the
                                 card's. Its label is set (label_source approver: the label check, relabel and the
                                 universe refresh never move it), a label_corrected fact recorded and the Overrides
                                 tab's row for the domain set (updated in place, or added), and the card withdrawn;
                                 a new card with that label's emails is posted in its slot at once (reprepare: the
                                 enrol run's own checks and prepare), or, when that cannot be, the next enrol proposes
                                 the company; for a label switched off, nothing. An unknown label gets the list.
                                 `approvals industry ID LABEL --live` is the same.
A card whose company's label is decided again after it was rendered, and no longer fits (verify_accounts' label
check, `relabel`, `labels set`), is withdrawn by the next poll_approvals before any ✅ on it is read (unfit_cards).
The 2-hour re-post and the 24-hour escalation of the reply desk do not apply to send approvals.

Expiry (a simple rule): an item may be approved on the UK day it was posted (send_day) and through
the next send day (expires_on). At the end of that day it is closed as expired by "system", its card
says so, and the account goes back to the queue: nothing was enrolled.

The data contract (the daily report reads it). hitl_items: one row per proposed contact, kind
"send_approval", with account_id, contact_id, slack_channel, slack_ts (the card), created_at; status
"open" while it waits (in any state), "sending" only while its lead is added, "handled" once closed,
with handled_at and handled_by (a Slack user id, "cli", or "system" for an expiry). Its payload always
has state (waiting, rejected, editing, sending, done), outcome ("" while open, then approved,
approved_edited, contact_rejected, company_rejected, expired or blocked), owner, mailbox, campaign,
lead (the Instantly lead, custom variables included), copy_version, angle, test_id, opener_arm,
opener_source, subject_arm (personal or copy: email 1's subject, render.subject_arm; Harry, 5 Oct 2026),
test_arm and test_name (the running copy test's arm, a or b, and its name, version_a or version_b; "" when the
contact is in no test) and test_note (why the account is left out of a running variant test, else ""; Harry,
7 Oct 2026; enrol/variants.py),
config_version, code_sha and copy_hash (what the card was rendered under, stamped on the contact at ✅, never
what is in force then; config_version.py, Harry, 7 Oct 2026; "" on a card posted before), industry, industry_group, role, tier, score, send_day (YYYY-MM-DD, UK), edited,
original (the first custom variables once edited, else {}) and reason (why blocked or expired, else
""); and the keys this package keeps for itself (expires_on, the card's facts, the steps as text and as
editable source, the render variables, approve_ts, seen_ts, choices_ts, contact_slot (2 for a second
contact) and first_contact (who the first was), ...). Each closing decision is
one events row: event_id "send-approval:{item_id}", type "send_approval", approval = the outcome,
approved_by, account_id, contact_id, step 1, mailbox, occurred_at.

Dry-run (live_sending = no, the scheduler's state until Harry signs off): enrol writes no item and
posts at most PREVIEW_CARDS cards (with their thread) to the dev channel so Harry sees what they will
look like; its summary says how many would have been posted. poll_approvals reads Slack, works out each
decision and changes nothing. Live: every card is posted; a live enrol without
US_OUTBOUND_SLACK_BOT_TOKEN refuses, as there is nowhere to approve. Items whose card could not be
posted are posted by the next poll_approvals run, and the command line does the same work without
Slack: `us-outbound approvals list`, `approvals approve ID --live` and `approvals reject ID
--contact|--company --live` take the same close path, with approved_by "cli".

The package (9 Oct 2026, the refactoring scan's phase 6; until then one module of about 1950 lines). Every name
above is still `approvals.NAME`:
  model.py        the data contract's names, Item and its row (_save, _cas), items, waiting, expiry, find_item
  payload.py      build_payload: what enrol's prepared account becomes; editable, recipient_source, history
  cards.py        the card and its thread's messages as Block Kit, from the payload alone
  thread.py       posting the card and notes in its thread, seeding reactions, propose; parse_command, parse_edit
  transitions.py  the decisions that close or hold an item (_close, _hold, reject, drop_contact, drop_company,
                  expire, withdraw)
  edit.py         ✏️: start_edit, render_edit, apply_edit
  checks.py       what a ✅ finds before the add: eligibility, recheck and its holds and blocks
  add.py          ✅: send, and what Instantly's answer leads to (_added, _refused, _plan_full)
  relabel.py      an approver's "industry: …" (set_industry, reprepare, repropose) and cards a label decision has
                  overtaken (label_unfit, unfit_cards, withdraw_unfit)
  polling.py      poll: the send-approval pass of poll_approvals, and an add a run left unfinished (_stuck)
  commands.py     `us-outbound approvals approve | reject | industry | redo`
"""

from us_outbound.enrol.approvals.add import (
    _prepared,
    send,
)
from us_outbound.enrol.approvals.cards import (
    card,
    card_copy_level,
    choices_text,
    edit_help,
    followups,
    industry_lines,
    opener_label,
    test_label,
    version_message,
)
from us_outbound.enrol.approvals.checks import (
    Recheck,
    eligibility,
    instantly_send_day,
    recheck,
)
from us_outbound.enrol.approvals.commands import (
    REDO_WHY,
    approve,
    industry_item,
    redo,
    reject_item,
)
from us_outbound.enrol.approvals.edit import (
    apply_edit,
    render_edit,
    start_edit,
    _templated,
)
from us_outbound.enrol.approvals.model import (
    ADDING,
    APOLLO_PERSON_URL,
    APPROVALS_CLI_JOB,
    APPROVED,
    APPROVED_EDITED,
    BLOCKED,
    COMPANY_REACTIONS,
    COMPANY_REJECTED,
    CONTACT_REACTIONS,
    CONTACT_REJECTED,
    DONE,
    EDITING,
    EDIT_REACTIONS,
    EVENT_PREFIX,
    EVENT_TYPE,
    EXPIRED,
    FOLLOW_UP_DAYS,
    HANDLED,
    HOLD_BLACKOUT,
    HOLD_CAMPAIGN,
    HOLD_HUBSPOT,
    HOLD_INSTANTLY,
    HOLD_LIVE,
    HOLD_OPTOUT,
    HOLD_PLAN,
    HOLD_REPLIES,
    HOLD_STOP,
    HOLD_STOP_RULE,
    Item,
    KIND,
    LIST_LIMIT,
    NO_SLACK,
    OPEN,
    OUTCOMES,
    PICK_FACT,
    PICK_SOURCE,
    PREVIEW_CARDS,
    REJECTED,
    REJECT_REACTIONS,
    SECTION_CHARS,
    SEED_APPROVE,
    SEED_CHOICES,
    SENDING,
    STUCK_AFTER,
    SYSTEM,
    TABLE,
    WAITING,
    Waiting,
    expires_on,
    find_item,
    is_expired,
    is_second,
    items,
    list_items,
    waiting,
)
from us_outbound.enrol.approvals.payload import (
    build_payload,
    editable,
    history,
    recipient_source,
)
from us_outbound.enrol.approvals.polling import (
    poll,
)
from us_outbound.enrol.approvals.relabel import (
    label_unfit,
    reprepare,
    repropose,
    set_industry,
    unfit_cards,
    withdraw_unfit,
)
from us_outbound.enrol.approvals.thread import (
    _COMMANDS,
    Command,
    parse_command,
    parse_edit,
    post_card,
    post_followups,
    propose,
    slack_for,
    slack_or_none,
)
from us_outbound.enrol.approvals.transitions import (
    drop_company,
    drop_contact,
    expire,
    _hold,
    reject,
    withdraw,
)

__all__ = [
    "ADDING", "APOLLO_PERSON_URL", "APPROVALS_CLI_JOB", "APPROVED", "APPROVED_EDITED", "BLOCKED", "_COMMANDS",
    "COMPANY_REACTIONS", "COMPANY_REJECTED", "CONTACT_REACTIONS", "CONTACT_REJECTED", "Command", "DONE", "EDITING",
    "EDIT_REACTIONS", "EVENT_PREFIX", "EVENT_TYPE", "EXPIRED", "FOLLOW_UP_DAYS", "HANDLED", "HOLD_BLACKOUT",
    "HOLD_CAMPAIGN", "HOLD_HUBSPOT", "HOLD_INSTANTLY", "HOLD_LIVE", "HOLD_OPTOUT", "HOLD_PLAN", "HOLD_REPLIES",
    "HOLD_STOP", "HOLD_STOP_RULE", "Item", "KIND", "LIST_LIMIT", "NO_SLACK", "OPEN", "OUTCOMES", "PICK_FACT",
    "PICK_SOURCE", "PREVIEW_CARDS", "REDO_WHY", "REJECTED", "REJECT_REACTIONS", "Recheck", "SECTION_CHARS",
    "SEED_APPROVE", "SEED_CHOICES", "SENDING", "STUCK_AFTER", "SYSTEM", "TABLE", "WAITING", "Waiting",
    "apply_edit", "approve", "build_payload", "card", "card_copy_level", "choices_text", "drop_company", "drop_contact",
    "edit_help", "editable", "eligibility", "expire", "expires_on", "find_item", "followups", "history", "_hold",
    "industry_item", "industry_lines", "instantly_send_day", "is_expired", "is_second", "items", "label_unfit",
    "list_items", "opener_label", "parse_command", "parse_edit", "poll", "post_card", "post_followups", "_prepared",
    "propose", "recheck", "recipient_source", "redo", "reject", "reject_item", "render_edit", "reprepare", "repropose",
    "send", "set_industry", "slack_for", "slack_or_none", "start_edit", "_templated", "test_label",
    "unfit_cards", "version_message", "waiting", "withdraw", "withdraw_unfit",
]
