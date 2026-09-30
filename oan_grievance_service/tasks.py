"""Scheduled jobs. Registered in hooks.py under scheduler_events.

A background process monitors open grievances against their SLA deadlines. The
batch must finish inside 30 minutes, so each job filters on an indexed column and touches only the cases that need work.
"""

import frappe
from frappe.utils import now_datetime

from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
	current_level_of,
)
from oan_grievance_service.grievance_masters.doctype.grievance_role_level.grievance_role_level import (
	GrievanceRoleLevel,
)
from oan_grievance_service.services import constants as C
from oan_grievance_service.services import lifecycle, notifications, sla


def open_grievances_with_sla(extra_filters=None):
	filters = {
		"docstatus": 1,
		"sla_due_date": ["is", "set"],
		# A stopped clock has nothing left to remind anyone about.
		"status": ["not in", list(sla.states_in_category(sla.STOPPED))],
	}
	if extra_filters:
		filters.update(extra_filters)
	return frappe.get_all(
		"Grievance",
		filters=filters,
		fields=[
			"name",
			"ticket_number",
			"status",
			"sla_start_at",
			"sla_due_date",
			"on_hold_since",
			"total_hold_time",
			"reminder_50_sent",
			"reminder_80_sent",
			"escalated",
			"assigned_dept",
			"assigned_to",
			"service_category",
			"grievance_type",
			"contact_email",
			"contact_mobile",
			"submitter_name",
			"administrative_area",
			"administrative_unit",
			"sla_days",
		],
	)


def send_sla_reminders():
	"""Reminders to the assigned officer at 50% and 80% of the window."""
	sent = 0
	for row in open_grievances_with_sla():
		# A paused case is waiting on the submitter, so the officer has nothing to be
		# reminded about and the deadline has not moved yet.
		if row.on_hold_since:
			continue
		percent = sla.consumed_percent(row)

		if percent >= 80 and not row.reminder_80_sent:
			grievance = frappe.get_doc("Grievance", row.name)
			notifications.queue(grievance, C.EVENT_SLA_REMINDER_80)
			notifications.queue(grievance, C.EVENT_SLA_AT_RISK)
			grievance.db_set("reminder_80_sent", 1, update_modified=False)
			sent += 1
		elif percent >= 50 and not row.reminder_50_sent:
			grievance = frappe.get_doc("Grievance", row.name)
			notifications.queue(grievance, C.EVENT_SLA_REMINDER_50)
			grievance.db_set("reminder_50_sent", 1, update_modified=False)
			sent += 1

	return sent


def escalate_breached():
	"""Hand every overdue case one rung up the chain.

	One indexed read on `next_escalation_at` rather than a scan of every open case:
	a grievance carries its own next deadline, so the query is the schedule. Each case
	is escalated inside its own try block, because one unroutable grievance must not
	take the rest of the batch down with it (the 30-minute budget assumes the run
	completes).
	"""
	# Site-wide stop, one read per run. Per-category is the `auto_escalate` tick on the
	# SLA configuration, which works by leaving `next_escalation_at` unarmed.
	if frappe.conf.get("grievance_auto_escalation_enabled") is False:
		return 0

	due_now = frappe.get_all(
		"Grievance",
		filters={
			"docstatus": 1,
			"next_escalation_at": ["<=", now_datetime()],
			"on_hold_since": ["is", "not set"],
		},
		pluck="name",
	)

	escalated = 0
	for name in due_now:
		try:
			if sla.escalate(frappe.get_doc("Grievance", name)):
				escalated += 1
		except Exception:
			frappe.log_error(
				title="Grievance escalation failed",
				message=f"{name}\n\n{frappe.get_traceback()}",
			)

	return escalated


# How long a change request waits on someone whose rung sets no hours of its own.
DEFAULT_CHANGE_REQUEST_HOURS = 24


def forward_stale_change_requests():
	"""Hand every change request nobody has acted on one step up the hierarchy.

	A request waits as long as its approver's rung allows (`escalation_hours`), the
	same clock a case gets on that rung. Requests already in the admin queue stay put.
	"""
	from frappe.utils import add_to_date, get_datetime

	pending = frappe.get_all(
		"Grievance Change Request",
		filters={"status": "Pending", "pending_with": ["is", "set"]},
		fields=["name", "pending_with", "pending_since"],
	)
	hours_by_level = {rung.name: rung.escalation_hours for rung in GrievanceRoleLevel.get_chain()}
	default_hours = frappe.conf.get("grievance_change_request_hours") or DEFAULT_CHANGE_REQUEST_HOURS

	forwarded = 0
	for row in pending:
		hours = hours_by_level.get(current_level_of(row.pending_with)) or default_hours
		if (
			not row.pending_since
			or add_to_date(get_datetime(row.pending_since), hours=hours) > now_datetime()
		):
			continue
		try:
			frappe.get_doc("Grievance Change Request", row.name).forward()
			forwarded += 1
		except Exception:
			frappe.log_error(
				title="Change request forwarding failed",
				message=f"{row.name}\n\n{frappe.get_traceback()}",
			)

	return forwarded


def expire_state_timers():
	"""Act on every case that has sat in its current state longer than its timer allows.

	The deadline was stamped on entering the state (sla.arm_state_timer). The timer is
	read again here rather than trusted from then, so a row an admin has since removed
	simply lets the case go.
	"""
	from oan_grievance_service.grievance_management.doctype.grievance_timeline.grievance_timeline import (
		GrievanceTimeline,
	)

	expired = frappe.get_all(
		"Grievance",
		filters={"docstatus": 1, "state_deadline": ["<", now_datetime()]},
		pluck="name",
	)

	for name in expired:
		try:
			grievance = frappe.get_doc("Grievance", name)
			state = grievance.workflow_state
			timer = sla.state_timer(grievance.service_category, state)
			grievance.db_set("state_deadline", None, update_modified=False)
			if not timer:
				continue

			if timer[1] == "Escalate":
				sla.escalate(grievance, reason=f"No action while {state}")
				continue

			if state == C.STATE_RESOLVED:
				reason = "Closed - resolution period elapsed without objection"
				body = "Grievance auto-closed: resolution period elapsed without objection."
			elif state == C.STATE_MORE_INFO_NEEDED:
				reason = "Closed - no response to information request"
				body = "Grievance auto-closed: no response to information request."
			else:
				reason = f"Closed - no activity while {state}"
				body = f"Grievance auto-closed: no activity while {state}."

			grievance.db_set("closure_reason", reason, update_modified=False)
			lifecycle.transition(
				grievance,
				"Auto Close",
				note=reason,
				automated=True,
				notify=True,
				closure_type="auto_closed",
			)
			GrievanceTimeline.record(
				grievance=grievance.name,
				entry_type="status_change",
				is_internal=False,
				body=body,
				author_user=None,
			)
		except Exception:
			frappe.log_error(
				title="Grievance state timer failed",
				message=f"{name}\n\n{frappe.get_traceback()}",
			)

	return len(expired)


def dispatch_notifications():
	"""Drain the notification queue."""
	return notifications.dispatch_queued()


def scan_pending_attachments():
	"""Drain the attachment scan queue.

	Nothing is served to an officer while a row is still Pending, so a backlog
	here is a usability problem rather than a safety one.
	"""
	from oan_grievance_service.services import scanning

	return scanning.scan_pending()


def drain_routing_queue():
	"""Drain unrouted submitted cases from the routing queue without deleting them."""
	from oan_grievance_service.services import routing

	return routing.drain_routing_queue()


def purge_expired_drafts():
	"""Daily: clear abandoned drafts that expired without being submitted."""
	from oan_grievance_service.api.v1 import draft

	return draft.purge_expired_drafts()


def refresh_dashboard_rollup():
	"""Every 15 minutes: bring the dashboard rollups up to date (last few days)."""
	from oan_grievance_service.services import dashboard_rollup

	return dashboard_rollup.refresh()


def rebuild_dashboard_rollup():
	"""Nightly: rebuild every day of the dashboard rollup, not just the last few."""
	from oan_grievance_service.services import dashboard_rollup

	return dashboard_rollup.refresh(full=True)


def hourly():
	"""Entry point wired to the hourly scheduler event."""
	send_sla_reminders()
	scan_pending_attachments()
	drain_routing_queue()
	escalate_breached()
	expire_state_timers()
	dispatch_notifications()


def daily():
	"""Entry point wired to the daily scheduler event."""
	purge_expired_drafts()
