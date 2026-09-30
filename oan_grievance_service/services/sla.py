"""SLA and escalation management.

Reminders at 50% and 80% of the window, and escalation up the role-level chain on
breach.

The clock starts from the creation date/time of the grievance.

What each workflow state does to the clock is its SLA category, set per state in
setup/install.py (WORKFLOW_STATES): Running, Paused or Stopped. A state can also carry
a timer - how long a case may sit there - configured per service category on the SLA
Configuration's State Timers.

What belongs here: the SLA clock (start, pause, resume, stop, consumed percentage, working days calculation),
state timers, and walking a case up the escalation chain.

What does not belong here: looking up who holds which rung or who reports to whom.
Those are RBAC assignment queries and live in the Grievance RBAC Assignment module.
Scheduling lives in tasks.py; this module only answers what a run should do.
"""

import math
from dataclasses import dataclass
from datetime import timedelta

import frappe
from frappe.model.workflow import get_workflow
from frappe.utils import add_days, add_to_date, get_datetime, getdate, now_datetime

from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
	current_level_of,
	find_officer_by_role_level,
	get_officer_supervisor,
)
from oan_grievance_service.grievance_management.doctype.grievance_timeline.grievance_timeline import (
	GrievanceTimeline,
)
from oan_grievance_service.grievance_masters.doctype.grievance_role_level.grievance_role_level import (
	GrievanceRoleLevel,
)
from oan_grievance_service.services import constants as C

CLOCK_START_ASSIGNMENT = "assignment"
CLOCK_START_CREATION = "creation"

RUNNING = "Running"
PAUSED = "Paused"
STOPPED = "Stopped"

CACHE_KEY_DEFAULT_HOLIDAY_LIST = "grievance_default_holiday_list"
CACHE_KEY_HOLIDAYS_PREFIX = "grievance_holidays_"


@dataclass(frozen=True, slots=True)
class SLAPolicy:
	"""Resolved SLA policy configuration for a service category."""

	name: str
	sla_days: int
	auto_escalate: bool
	auto_escalation_threshold: int
	first_response_hours: int | None = None
	update_cadence_hours: int | None = None
	remand_execution_hours: int | None = None
	holiday_list: str | None = None


def get_default_holiday_list() -> str | None:
	"""Returns the default Grievance Holiday List name if one is marked default."""
	cached = frappe.cache.get_value(CACHE_KEY_DEFAULT_HOLIDAY_LIST)
	if cached is not None:
		return cached or None
	default_list = frappe.db.get_value("Grievance Holiday List", {"is_default": 1}, "name")
	frappe.cache.set_value(CACHE_KEY_DEFAULT_HOLIDAY_LIST, default_list or "")
	return default_list


def get_holiday_dates(holiday_list_name: str | None) -> set:
	"""Return set of datetime.date objects for all holidays in the given holiday list."""
	if not holiday_list_name:
		return set()
	cache_key = f"{CACHE_KEY_HOLIDAYS_PREFIX}{holiday_list_name}"
	cached = frappe.cache.get_value(cache_key)
	if cached is not None:
		return {getdate(d) for d in cached}

	if not frappe.db.exists("Grievance Holiday List", holiday_list_name):
		return set()

	rows = frappe.get_all(
		"Grievance Holiday",
		filters={"parent": holiday_list_name, "is_half_day": 0},
		pluck="holiday_date",
	)
	holidays_set = {getdate(d) for d in rows}
	frappe.cache.set_value(cache_key, [str(d) for d in holidays_set])
	return holidays_set


def is_holiday(date, holiday_list_name: str | None = None) -> bool:
	"""Check if a date is a holiday or weekly off."""
	h_list = holiday_list_name or get_default_holiday_list()
	if not h_list:
		return False
	holidays = get_holiday_dates(h_list)
	return getdate(date) in holidays


def calculate_working_deadline(start_datetime, sla_days: int, holiday_list_name: str | None = None):
	"""Calculate target due date adding working days, skipping holidays and weekly offs.

	Days allocated count from start_datetime (which defaults to creation date/time).
	Preserves the time component of start_datetime.
	"""
	if not start_datetime:
		start_datetime = now_datetime()
	start_dt = get_datetime(start_datetime)
	if not sla_days or sla_days <= 0:
		return start_dt

	h_list = holiday_list_name or get_default_holiday_list()
	holidays = get_holiday_dates(h_list) if h_list else set()

	if not holidays:
		return add_days(start_dt, sla_days)

	cur_dt = start_dt
	days_added = 0
	while days_added < sla_days:
		cur_dt = add_to_date(cur_dt, days=1)
		if cur_dt.date() not in holidays:
			days_added += 1

	return cur_dt


def resolve_policy(service_category) -> SLAPolicy | None:
	"""One policy per service category. Grievance type does not narrow the SLA."""
	rows = frappe.get_all(
		"Grievance SLA Configuration",
		filters={
			"service_category": service_category,
			"active": 1,
		},
		fields=[
			"name",
			"sla_days",
			"auto_escalate",
			"auto_escalation_threshold",
			"first_response_hours",
			"update_cadence_hours",
			"remand_execution_hours",
			"holiday_list",
		],
		limit=1,
	)
	if not rows:
		return None
	r = rows[0]
	return SLAPolicy(
		name=r.name,
		sla_days=r.sla_days or 0,
		auto_escalate=bool(r.auto_escalate),
		auto_escalation_threshold=r.auto_escalation_threshold or 100,
		first_response_hours=r.first_response_hours,
		update_cadence_hours=r.update_cadence_hours,
		remand_execution_hours=r.remand_execution_hours,
		holiday_list=r.holiday_list or get_default_holiday_list(),
	)


def start_clock(grievance):
	"""Stamp the SLA window onto the grievance. Idempotent."""
	if grievance.sla_due_date:
		return

	policy = resolve_policy(grievance.service_category)
	if not policy or not policy.sla_days:
		return

	started = get_datetime(grievance.sla_start_at or grievance.creation or now_datetime())
	due = calculate_working_deadline(started, policy.sla_days, holiday_list_name=policy.holiday_list)
	grievance.db_set(
		{
			"sla_days": policy.sla_days,
			"sla_start_at": started,
			"sla_due_date": due,
		},
		update_modified=False,
	)
	arm_escalation(grievance, policy=policy)


def recalculate_sla_on_category_change(grievance, old_category=None, new_category=None):
	"""Recalculate SLA due date and escalation timestamps when category changes.

	Recalculates target resolution time based on the new category's policy starting from
	the creation / start date, taking into account any banked hold time and holidays.
	"""
	cat = new_category or grievance.service_category
	policy = resolve_policy(cat)
	if not policy or not policy.sla_days:
		return

	started = get_datetime(grievance.sla_start_at or grievance.creation or now_datetime())
	new_due = calculate_working_deadline(started, policy.sla_days, holiday_list_name=policy.holiday_list)

	banked = grievance.get("total_hold_time") or 0
	if banked:
		new_due = add_to_date(new_due, seconds=banked)

	updates = {
		"sla_days": policy.sla_days,
		"sla_start_at": started,
		"sla_due_date": new_due,
		"reminder_50_sent": 0,
		"reminder_80_sent": 0,
	}

	grievance.db_set(updates, update_modified=False)
	arm_escalation(grievance, policy=policy)


def reset_clock(grievance):
	"""Restart the SLA window from now when category changes on reassignment."""
	policy = resolve_policy(grievance.service_category)
	now = now_datetime()
	updates = {
		"sla_start_at": now,
		"total_hold_time": 0,
		"reminder_50_sent": 0,
		"reminder_80_sent": 0,
		"escalated": 0,
	}
	if policy and policy.sla_days:
		due = calculate_working_deadline(now, policy.sla_days, holiday_list_name=policy.holiday_list)
		updates["sla_days"] = policy.sla_days
		updates["sla_due_date"] = due
	else:
		updates["sla_days"] = 0
		updates["sla_due_date"] = None

	grievance.db_set(updates, update_modified=False)
	for k, v in updates.items():
		setattr(grievance, k, v)
	arm_escalation(grievance, policy=policy)


def arm_escalation(grievance, policy):
	"""Point the escalation clock at the first bump.

	Until a case has escalated once the next bump is a fraction of its window, so this
	is re-run whenever the deadline moves (resume from hold, approved deferral, category change). Once
	the case starts climbing, the rung's own hours own the schedule and the deadline is
	no longer the thing being waited on.

	Turning `auto_escalate` off simply leaves the clock unarmed. That is the whole
	switch: the batch selects on `next_escalation_at`, so a null is invisible to it,
	and no per-case policy lookup is needed at escalation time. The SLA window, the
	reminders and the compliance reporting all carry on untouched.
	"""
	if not policy or not policy.auto_escalate:
		grievance.db_set("next_escalation_at", None, update_modified=False)
		return

	start = get_datetime(grievance.sla_start_at) if grievance.sla_start_at else None
	due = get_datetime(grievance.sla_due_date)
	threshold = policy.auto_escalation_threshold or 100

	if start and 0 < threshold < 100:
		# Hand the case up before the deadline, while there is still time to save it.
		consumed = (due - start).total_seconds() * threshold / 100
		at = add_to_date(start, seconds=int(consumed))
	else:
		at = due

	grievance.db_set("next_escalation_at", at, update_modified=False)


def open_hold_seconds(grievance):
	"""Seconds in the hold that is still running. Zero when the clock is not paused."""
	on_hold = grievance.get("on_hold_since")
	if not on_hold:
		return 0
	return max(0, int((now_datetime() - get_datetime(on_hold)).total_seconds()))


def sla_category_of(state):
	"""What the Grievance Workflow says a state does to the clock. Unset means Running."""
	for row in get_workflow("Grievance").states:
		if row.state == state:
			return row.get("sla_category") or RUNNING
	return RUNNING


def states_in_category(category):
	return {
		row.state
		for row in get_workflow("Grievance").states
		if (row.get("sla_category") or RUNNING) == category
	}


def on_status_change(grievance, to_state):
	"""Start, pause, resume or stop the clock for the state the case just entered.

	The clock starts when the case reaches a department. A Paused state holds it, and
	the deadline is pushed out by the hold when the case leaves. A Stopped state ends
	it: any hold is banked first, so the deadline the outcome is judged against stays
	fair, and escalation is disarmed for good.
	"""
	if to_state == C.STATE_ASSIGNED:
		start_clock(grievance)

	category = sla_category_of(to_state)
	if category == PAUSED:
		if grievance.sla_due_date and not grievance.on_hold_since:
			grievance.db_set("on_hold_since", now_datetime(), update_modified=False)
		return

	resume_clock(grievance)
	if category == STOPPED and grievance.next_escalation_at:
		grievance.db_set("next_escalation_at", None, update_modified=False)


def state_timer(service_category, state):
	"""(hours, on_expiry) for a state, or None when it has no timer.

	Read from the category's SLA Configuration. Resolved always has one: with
	no row configured, the confirmation window falls back to the site's
	`grievance_confirmation_window_days`, then to seven days.
	"""
	config = (
		frappe.db.get_value(
			"Grievance SLA Configuration", {"service_category": service_category, "active": 1}
		)
		if service_category
		else None
	)
	if config:
		row = frappe.db.get_value(
			"Grievance State Timer",
			{"parenttype": "Grievance SLA Configuration", "parent": config, "workflow_state": state},
			["hours", "on_expiry"],
			as_dict=True,
		)
		if row:
			return row.hours, row.on_expiry
	if state == C.STATE_RESOLVED:
		days = int(frappe.conf.get("grievance_confirmation_window_days") or C.DEFAULT_CONFIRMATION_DAYS)
		return days * 24, "Auto Close"
	return None


def arm_state_timer(grievance, state):
	"""Stamp when the case's new state runs out, or clear the previous state's deadline."""
	timer = state_timer(grievance.service_category, state)
	deadline = add_to_date(now_datetime(), hours=timer[0]) if timer else None
	if deadline or grievance.get("state_deadline"):
		grievance.db_set("state_deadline", deadline, update_modified=False)


def resume_clock(grievance):
	"""Bank the hold and push the deadline out by the same amount.

	Two writes total per hold: one on pause, one here. The reminder flags
	are deliberately left alone, because consumed percentage is unchanged across a resume:
	the window and the elapsed time both move by the hold duration.
	"""
	if not grievance.on_hold_since:
		return

	held = open_hold_seconds(grievance)
	updates = {
		"total_hold_time": (grievance.total_hold_time or 0) + held,
		"on_hold_since": None,
	}

	if held and grievance.sla_due_date:
		updates["sla_due_date"] = add_to_date(get_datetime(grievance.sla_due_date), seconds=held)
		# The escalation clock is pushed by the same amount rather than re-armed, so a
		# rung that was part-way through its own hours keeps the remainder instead of
		# being overtaken the moment the case comes off hold.
		if grievance.next_escalation_at:
			updates["next_escalation_at"] = add_to_date(
				get_datetime(grievance.next_escalation_at), seconds=held
			)

	grievance.db_set(updates, update_modified=False)
	return held


def consumed_percent(grievance):
	"""The SLA tracker's consumed percentage, calculated hourly with upper limit (ceiling), excluding hold time."""
	start_val = grievance.get("sla_start_at")
	due_val = grievance.get("sla_due_date")
	if not (start_val and due_val):
		return 0
	start = get_datetime(start_val)
	due = get_datetime(due_val)
	banked = grievance.get("total_hold_time") or 0

	window_seconds = (due - start).total_seconds() - banked
	if window_seconds <= 0:
		return 100

	elapsed_seconds = (now_datetime() - start).total_seconds() - banked - open_hold_seconds(grievance)
	if elapsed_seconds <= 0:
		return 0

	# Hourly calculation taking upper limit (ceil)
	window_hours = max(1, math.ceil(window_seconds / 3600))
	elapsed_hours = math.ceil(elapsed_seconds / 3600)
	percent = math.ceil((elapsed_hours / window_hours) * 100)
	return max(0, min(percent, 999))


def higher_authority_of(user, department=None, administrative_area=None, log_unstaffed_for=None):
	"""The person one step above `user` for a department and area, and their rung.

	1. The user's direct supervisor (reports_to), when they sit higher up the chain or
	   hold no rung at all.
	2. Otherwise whoever holds the next rung for the department and area.

	A user with no rung (a submitter, or nobody) enters at the most junior rung.
	Returns (None, None) at the top of the chain; `log_unstaffed_for` names the
	grievance to report when a rung has nobody on it.
	"""
	chain = GrievanceRoleLevel.get_chain()
	if not chain:
		return None, None

	rank = {rung.name: index for index, rung in enumerate(chain)}
	current_level = current_level_of(user)
	current_rank = rank.get(current_level, -1)
	if current_level and current_level not in rank:
		# Sits on a rung that has since been deactivated: nowhere defined to climb to.
		return None, None
	if current_rank + 1 >= len(chain):
		return None, None
	supervisor = get_officer_supervisor(user, department=department, administrative_area=administrative_area)
	if supervisor and supervisor != user:
		sup_level = current_level_of(supervisor)
		if not sup_level:
			return supervisor, chain[current_rank + 1]
		if rank.get(sup_level, -1) > current_rank:
			return supervisor, chain[rank[sup_level]]

	for next_level in chain[current_rank + 1 :]:
		officer = find_officer_by_role_level(
			next_level.name, department=department, administrative_area=administrative_area
		)
		if officer and officer != user:
			return officer, next_level
		if not officer and log_unstaffed_for:
			frappe.log_error(
				title=f"Grievance escalation rung unstaffed: {next_level.name}",
				message=(
					f"No active Grievance RBAC Assignment holds role level '{next_level.name}' for "
					f"department '{department}' and administrative area '{administrative_area}'. "
					f"Grievance {log_unstaffed_for} skipped this rung."
				),
			)

	return None, None


def _first_escalation(grievance):
	"""`escalated_at` keeps the first escalation; later rungs leave it alone."""
	return {} if grievance.get("escalated_at") else {"escalated_at": now_datetime()}


def escalate(grievance, reason=None, reassign=True):
	"""Move the case one rung up the chain and re-arm the clock.

	`escalated` stays a flag, never a status, so the lifecycle stage is untouched.
	Returns the user the case was handed to, or None when it could not move.
	"""
	from oan_grievance_service.services import notifications

	target, level = higher_authority_of(
		grievance.assigned_to,
		department=grievance.assigned_dept,
		administrative_area=grievance.administrative_area,
		log_unstaffed_for=grievance.name,
	)
	if not target:
		updates = {
			"escalated": 1,
			"next_escalation_at": None,
			**_first_escalation(grievance),
		}
		grievance.db_set(updates, update_modified=False)

		body = "Case escalated (no higher authority configured for reassignment)"
		if reason:
			body += f": {reason}"
		GrievanceTimeline.record(
			grievance=grievance.name,
			entry_type="escalation",
			is_internal=False,
			body=body,
			author_user=frappe.session.user if frappe.session.user != "Guest" else None,
		)

		if grievance.assigned_to:
			notifications.queue(grievance, C.EVENT_SLA_BREACH, recipient_override=grievance.assigned_to)
		return grievance.assigned_to or True

	# The rung the case just landed on owns the next deadline. No hours means this is a
	# terminal rung and the ladder stops here.
	hours = level.escalation_hours or 0
	updates = {
		"escalated": 1,
		"next_escalation_at": add_to_date(now_datetime(), hours=hours) if hours else None,
		**_first_escalation(grievance),
	}
	if reassign:
		updates["assigned_to"] = target
	grievance.db_set(updates, update_modified=False)

	level_role = level.level_name or level.name
	body = f"Case escalated to {level_role}"
	if reason:
		body += f": {reason}"
	GrievanceTimeline.record(
		grievance=grievance.name,
		entry_type="escalation",
		is_internal=False,
		body=body,
		author_user=frappe.session.user if frappe.session.user != "Guest" else None,
	)

	notifications.queue(grievance, C.EVENT_SLA_BREACH, recipient_override=target)
	return target
