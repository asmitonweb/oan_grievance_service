# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

"""Dashboard rollups: the only reader of Grievance for analytics.

The dashboards never aggregate `tabGrievance` on a request. The scheduler folds it
into two small tables every 15 minutes, and services.dashboard answers every chart
from those:

- Grievance Stat Daily: events per day (filed, resolved, rejected, first
  escalated, feedback) by region, category and department. Events never move
  once they happen, so a refresh rebuilds only the last few days; the nightly run
  rebuilds all of them to absorb anything later edited by hand.
- Grievance Stat Snapshot: how many cases stood in each status on a day, by the
  same dimensions plus the escalation flag and SLA state. Today's rows are
  replaced on every refresh; earlier days stay as they were at midnight, which is
  the only record of past status counts.

Only counts, sums and minimums are stored. Rates and averages are worked out when
a chart is read, so every filter combination stays exact.
"""

import frappe
from frappe.utils import add_days, add_to_date, get_datetime, getdate, now_datetime

from oan_grievance_service.services import constants as C
from oan_grievance_service.services import sla

DAILY = "Grievance Stat Daily"
SNAPSHOT = "Grievance Stat Snapshot"

# Where the time of the last completed refresh is kept, as a system default: it is
# what every chart reports as its as_of.
AS_OF_DEFAULT = "grievance_dashboard_as_of"
LOCK_KEY = "grievance_dashboard_rollup"

# An incremental refresh rebuilds this many days, today included. More than one,
# so a run that failed around midnight is repaired by the next.
INCREMENTAL_DAYS = 3
AT_RISK_HOURS = 24
SATISFIED_RATING = 4
_EPOCH = "2000-01-01"

DAILY_METRICS = (
	"submitted_count",
	"resolved_count",
	"resolved_on_time_count",
	"resolved_breached_count",
	"resolution_hours_sum",
	"rejected_count",
	"escalated_count",
	"feedback_count",
	"feedback_satisfied_count",
)
SNAPSHOT_FIELDS = (
	"status",
	"region",
	"service_category",
	"assigned_dept",
	"escalated",
	"sla_state",
	"grievance_count",
	"dup_pending_count",
	"oldest_open_creation",
)

# The SQL below is assembled once, here, from constant fragments; no request
# value is ever formatted into it -- windows and states go in as parameters.
# GROUP BY is positional: MariaDB resolves a GROUP BY name against the tables
# before the select list, so `assigned_dept` would group on the raw column and
# split NULL from '' after COALESCE had merged them.
#
# A grievance's region is the Region-level ancestor of its filing area, found
# through the area's own lft rather than the copy on Grievance, which goes stale
# when the tree is rebuilt. It is stored as the region's P-code.
_AREA_JOIN = """
	JOIN `tabGrievance Administrative Area` a ON a.name = g.administrative_area
	LEFT JOIN `tabGrievance Administrative Area` r
		ON r.level_name = 'Region' AND a.lft BETWEEN r.lft AND r.rgt"""
_DIMENSIONS = (
	"COALESCE(r.code, '') AS region, g.service_category, COALESCE(g.assigned_dept, '') AS assigned_dept"
)


def _daily_sql(day_column, measures, where, source="`tabGrievance` g"):
	return f"""
		SELECT DATE({day_column}) AS stat_date, {_DIMENSIONS}, {measures}
		FROM {source}{_AREA_JOIN}
		WHERE {day_column} >= %(start)s AND {day_column} < %(end)s AND {where}
		GROUP BY 1, 2, 3, 4"""


_SUBMITTED_SQL = _daily_sql("g.creation", "COUNT(*) AS submitted_count", "g.status != 'Draft'")
_RESOLVED_SQL = _daily_sql(
	"g.resolved_at",
	"""COUNT(*) AS resolved_count,
		SUM(g.sla_due_date IS NOT NULL AND g.resolved_at <= g.sla_due_date) AS resolved_on_time_count,
		SUM(g.sla_due_date IS NOT NULL AND g.resolved_at > g.sla_due_date) AS resolved_breached_count,
		SUM(TIMESTAMPDIFF(SECOND, g.creation, g.resolved_at)) / 3600 AS resolution_hours_sum""",
	"g.status IN %(resolved)s",
)
# Rejected is terminal and a rejected case is never saved again, so its last
# modification is when it was rejected.
_REJECTED_SQL = _daily_sql("g.modified", "COUNT(*) AS rejected_count", "g.status = 'Rejected'")
_ESCALATED_SQL = _daily_sql("g.escalated_at", "COUNT(*) AS escalated_count", "g.status != 'Draft'")
_FEEDBACK_SQL = _daily_sql(
	"f.submitted_at",
	"""COUNT(*) AS feedback_count,
		SUM(f.rating >= %(satisfied)s) AS feedback_satisfied_count""",
	"g.status != 'Draft'",
	source="`tabGrievance Feedback` f JOIN `tabGrievance` g ON g.name = f.grievance",
)

_SNAPSHOT_SQL = f"""
	SELECT g.status, {_DIMENSIONS}, g.escalated,
		CASE
			WHEN g.status NOT IN %(open)s THEN ''
			WHEN g.status IN %(paused)s THEN 'paused'
			WHEN g.sla_due_date IS NULL THEN 'no_sla'
			WHEN g.sla_due_date < %(now)s THEN 'breached'
			WHEN g.sla_due_date < %(at_risk)s THEN 'at_risk'
			ELSE 'ok'
		END AS sla_state,
		COUNT(*) AS grievance_count,
		SUM(d.grievance IS NOT NULL AND g.status IN %(open)s) AS dup_pending_count,
		MIN(CASE WHEN g.status IN %(open)s AND g.escalated = 0 THEN g.creation END) AS oldest_open_creation
	FROM `tabGrievance` g{_AREA_JOIN}
	LEFT JOIN (
		SELECT DISTINCT grievance FROM `tabGrievance Duplicate` WHERE is_confirmed = 0
	) d ON d.grievance = g.name
	WHERE g.status != 'Draft'
	GROUP BY 1, 2, 3, 4, 5, 6"""


def refresh(full=False):
	"""Bring both rollups up to date. Returns what it did, or None if a run was already going.

	`full` rebuilds every day of Grievance Stat Daily instead of the last few.
	Snapshot history is never rebuilt: a past day's status counts cannot be
	recovered from current data, which is why they are kept.
	"""
	# A database named lock: held by this connection, so a worker that dies
	# mid-run releases it with its connection instead of blocking later runs.
	lock_name = f"{frappe.local.site}:{LOCK_KEY}"
	if not frappe.db.sql("SELECT GET_LOCK(%s, 0)", lock_name)[0][0]:
		frappe.logger().info("Grievance dashboard rollup skipped: another refresh is running")
		return None
	try:
		now = now_datetime()
		today = getdate(now)
		start = getdate(_EPOCH) if full else add_days(today, -(INCREMENTAL_DAYS - 1))
		daily_rows = _rebuild_daily(start, add_days(today, 1))
		snapshot_rows = _rebuild_snapshot(today, now)
		frappe.db.set_default(AS_OF_DEFAULT, str(now))
		return {"as_of": now, "from": start, "daily_rows": daily_rows, "snapshot_rows": snapshot_rows}
	finally:
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", lock_name)


def as_of():
	"""When the rollups were last refreshed, or None if they never have been."""
	value = frappe.db.get_default(AS_OF_DEFAULT)
	return get_datetime(value) if value else None


def ensure_built():
	"""after_migrate: queue a first full build on a site that has none yet."""
	if frappe.flags.in_test or frappe.db.exists(DAILY) or not frappe.db.exists("Grievance"):
		return
	frappe.enqueue(
		"oan_grievance_service.services.dashboard_rollup.refresh",
		full=True,
		queue="long",
		job_id=LOCK_KEY,
		deduplicate=True,
		enqueue_after_commit=True,
	)


def _rebuild_daily(start, end):
	params = {
		"start": start,
		"end": end,
		"resolved": C.RESOLVED_STATES,
		"satisfied": SATISFIED_RATING,
	}
	merged = {}
	for sql in (_SUBMITTED_SQL, _RESOLVED_SQL, _REJECTED_SQL, _ESCALATED_SQL, _FEEDBACK_SQL):
		for row in frappe.db.sql(
			sql, params, as_dict=True
		):  # nosemgrep: frappe-semgrep-rules.rules.frappe-sql-format-injection
			key = (row.stat_date, row.region, row.service_category, row.assigned_dept)
			target = merged.setdefault(key, dict.fromkeys(DAILY_METRICS, 0))
			for metric in DAILY_METRICS:
				if metric in row:
					target[metric] = row[metric] or 0

	frappe.db.delete(DAILY, {"stat_date": [">=", start]})
	_insert(
		DAILY,
		("stat_date", "region", "service_category", "assigned_dept", *DAILY_METRICS),
		[(*key, *(metrics[m] for m in DAILY_METRICS)) for key, metrics in merged.items()],
	)
	return len(merged)


def _rebuild_snapshot(today, now):
	params = {
		"open": C.OPEN_STATES,
		# An empty IN () is a syntax error; no state is named "".
		"paused": tuple(sla.states_in_category(sla.PAUSED)) or ("",),
		"now": now,
		"at_risk": add_to_date(now, hours=AT_RISK_HOURS),
	}
	rows = frappe.db.sql(
		_SNAPSHOT_SQL, params, as_dict=True
	)  # nosemgrep: frappe-semgrep-rules.rules.frappe-sql-format-injection
	frappe.db.delete(SNAPSHOT, {"snapshot_date": today})
	_insert(
		SNAPSHOT,
		("snapshot_date", *SNAPSHOT_FIELDS),
		[(today, *(row[f] for f in SNAPSHOT_FIELDS)) for row in rows],
	)
	return len(rows)


def _insert(doctype, fields, values):
	if not values:
		return
	now = now_datetime()
	frappe.db.bulk_insert(
		doctype,
		("name", "creation", "modified", "owner", "modified_by", *fields),
		[
			(frappe.generate_hash(length=12), now, now, "Administrator", "Administrator", *row)
			for row in values
		],
	)
