"""Document event handlers registered in hooks.py.

These are the joins between a saved record and the workflow, kept
out of the doctype controllers so the sequence is readable in one place.
"""

import frappe
from frappe.model.workflow import get_workflow
from frappe.utils import now_datetime

from oan_grievance_service.grievance_management.doctype.grievance_timeline.grievance_timeline import (
	GrievanceTimeline,
)
from oan_grievance_service.services import constants as C
from oan_grievance_service.services import lifecycle, notifications, sla

# Workflow moves
# --------------
# Frappe's engine drives a move by saving, submitting or cancelling the Grievance, so
# the Grievance controller calls `after_workflow_action` once the new
# state is written.


def after_workflow_action(doc, from_state):
	"""Record the move and carry out what arriving in a state requires."""
	context = frappe.flags.grievance_transition or frappe._dict()
	to_state = doc.workflow_state

	# A desk button arrives with no context; the Workflow still knows which
	# action joins the two states, so the trail names it either way.
	if not context.action:
		context.action = next(
			(
				row.action
				for row in get_workflow(doc.doctype).transitions
				if row.state == from_state and row.next_state == to_state
			),
			None,
		)

	user = None if context.automated else frappe.session.user
	if user == "Guest":
		user = None

	# Refuses, and with it the whole move, when a reason is required and none came.
	history = frappe.get_doc(
		{
			"doctype": "Grievance Status History",
			"grievance": doc.name,
			"from_status": from_state,
			"to_status": to_state,
			"transition": context.action,
			"closure_type": context.closure_type,
			"is_automated": 1 if context.automated else 0,
			"changed_by": user,
			"timestamp": now_datetime(),
			"reason": context.reason,
			"notes": context.reason or context.note,
		}
	).insert(ignore_permissions=True)
	context.history = history

	sla.on_status_change(doc, to_state)
	sla.arm_state_timer(doc, to_state)
	stamp_resolution(doc, to_state)

	if context.get("notify", True):
		if to_state == C.STATE_IN_PROGRESS:
			if from_state in (C.STATE_ASSIGNED, C.STATE_SUBMITTED):
				notifications.queue(doc, C.EVENT_STATUS_IN_PROGRESS)
			elif from_state == C.STATE_RESOLVED or context.get("action") == "Reopen":
				notifications.queue(doc, C.EVENT_REOPENED)
		elif to_state == C.STATE_MORE_INFO_NEEDED:
			notifications.queue(doc, C.EVENT_MORE_INFO_REQUESTED)
		elif to_state == C.STATE_RESOLVED:
			notifications.queue(doc, C.EVENT_CONFIRMED)
		elif to_state == C.STATE_CLOSED:
			if from_state == C.STATE_RESOLVED and (
				context.get("closure_type") == "auto_closed" or context.get("action") == "Auto Close"
			):
				notifications.queue(doc, C.EVENT_AUTO_CLOSED)
			else:
				notifications.queue(doc, C.EVENT_CLOSED)
		elif to_state == C.STATE_REJECTED:
			notifications.queue(doc, C.EVENT_STATUS_REJECTED)


def stamp_resolution(doc, to_state):
	"""Keep `resolved_at` on the moment the case last reached Resolved or Closed.

	Resolved then Closed keeps the first stamp: the case was resolved when the
	officer resolved it, not when the confirmation window ran out. A reopen clears
	it, so a case resolved twice counts once, on the day it was finally resolved.
	"""
	if to_state in C.RESOLVED_STATES:
		if not doc.resolved_at:
			doc.db_set("resolved_at", now_datetime(), update_modified=False)
	elif doc.resolved_at and to_state != C.STATE_REJECTED:
		doc.db_set("resolved_at", None, update_modified=False)


def response_after_insert(doc, method=None):
	"""The response outcome drives the next status."""
	grievance = frappe.get_doc("Grievance", doc.grievance)

	# response_date, responded_by, sequence and prior_status are filled in by the
	# controller before validation, because they are mandatory. The IP is captured here
	# because it is only meaningful for a request that actually reached the server.
	if getattr(frappe.local, "request_ip", None):
		doc.db_set("ip_address", frappe.local.request_ip, update_modified=False)

	# Record formal response in unified timeline spine
	GrievanceTimeline.record(
		grievance=grievance.name,
		entry_type="response",
		is_internal=False,
		body=doc.resolution_summary or doc.action_taken or f"Formal Response ({doc.response_type})",
		author_user=doc.responded_by or frappe.session.user,
		ref_doctype="Grievance Response",
		ref_docname=doc.name,
	)

	# Dynamic Master Resolution: the linked Grievance Response Type names the action.
	action = None
	if doc.response_type and frappe.db.exists("Grievance Response Type", doc.response_type):
		action = frappe.db.get_value("Grievance Response Type", doc.response_type, "workflow_action")

	if action and action in lifecycle.actions_available(grievance):
		lifecycle.transition(
			grievance,
			action,
			note=f"Response {doc.name} ({doc.response_type})",
		)

	doc.db_set("new_status", grievance.status, update_modified=False)

	# A response clears the escalation flag only. `next_escalation_at` keeps running:
	# an officer who answers and then sits on the case again must still be overtaken.
	if grievance.escalated:
		grievance.db_set("escalated", 0, update_modified=False)

	# structured_response_sent prompts the citizen to confirm resolution or reopen within
	# the confirmation window, so queue it only for resolution responses.
	if doc.response_type in ("Resolved", "Partially Resolved"):
		notifications.queue(grievance, C.EVENT_RESPONSE_SENT)
		doc.db_set({"notification_sent": 1, "notification_sent_at": now_datetime()}, update_modified=False)
