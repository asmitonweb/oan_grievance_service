# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

"""Dashboard charts: every analytics query the API serves lives in this module.

Each chart is a builder over the rollups (services.dashboard_rollup), or, for the
few admin-only charts that must show a real case, a small indexed query over
Grievance. Two views share the builders:

- admin: `GET /api/v1/charts`, Grievance Admin only; every chart, and live
  case detail where a chart has it.
- public: `GET /api/v1/charts/<chart_id>`, no login; only charts marked public,
  which are counts from the rollups and never name a case, a person or a text.

Results are cached per chart and parameters in Redis. A rollup chart's key
carries the rollup's as_of, so a refresh is a clean cut-over.
"""

import hashlib
import json
from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import date, timedelta

import frappe
from frappe.query_builder.functions import Min, Sum
from frappe.utils import add_months, get_datetime, getdate, now_datetime
from oan_auth_service.api.utils import to_tz_aware_iso

from oan_grievance_service.services import constants as C
from oan_grievance_service.services import dashboard_rollup as rollup

ROLLUP_TTL = 15 * 60
LIVE_TTL = 60
TITLE_LENGTH = 80

# Every state but Draft, in lifecycle order, so the donut always has the same
# segments whether or not a state currently holds a case.
STATUS_ORDER = (*C.OPEN_STATES, *C.RESOLVED_STATES, C.STATE_REJECTED)


@dataclass(frozen=True)
class Params:
	"""Chart parameters after validation. Empty tuples mean "all"."""

	region: tuple = ()
	service_category: tuple = ()
	assigned_dept: tuple = ()
	from_date: date | None = None
	to_date: date | None = None
	month: str | None = None
	granularity: str = "month"
	limit: int = 10

	def used_by(self, chart):
		"""Only the parameters a chart reads, so the others do not split its cache."""
		values = asdict(self)
		return {name: values[name] for name in sorted(chart.params)}


FILTERS = frozenset({"region", "service_category", "assigned_dept"})
PERIOD = FILTERS | {"from_date", "to_date"}


@dataclass(frozen=True)
class Chart:
	build: callable
	params: frozenset = FILTERS
	public: bool = True
	# "live" charts read Grievance directly (admin view only) and cache briefly.
	live_for_admin: bool = False


class Context:
	"""What a builder needs besides the parameters: the view and the snapshot day."""

	def __init__(self, params, admin):
		self.params = params
		self.admin = admin
		self.today = getdate(now_datetime())
		self.snapshot_date = _latest_snapshot_date()


# Reading the rollups
# -------------------


def _apply_filters(query, table, params):
	if params.region:
		query = query.where(table.region.isin(params.region))
	if params.service_category:
		query = query.where(table.service_category.isin(params.service_category))
	if params.assigned_dept:
		query = query.where(table.assigned_dept.isin(params.assigned_dept))
	return query


def _latest_snapshot_date():
	return _snapshot_on_or_before(getdate(now_datetime()))


def _snapshot(ctx, group_by, on_date=None, params=None):
	"""Summed snapshot rows for one day (default: the latest), grouped by `group_by`."""
	day = on_date or ctx.snapshot_date
	if not day:
		return []
	S = frappe.qb.DocType(rollup.SNAPSHOT)
	columns = [getattr(S, name) for name in group_by]
	query = (
		frappe.qb.from_(S)
		.select(
			*columns,
			Sum(S.grievance_count).as_("grievances"),
			Sum(S.dup_pending_count).as_("dup_pending"),
			Min(S.oldest_open_creation).as_("oldest_open"),
		)
		.where(S.snapshot_date == day)
		# Only today's workflow states, so every chart adds up to the same total even
		# while rows from a retired state are still being migrated.
		.where(S.status.isin(STATUS_ORDER))
	)
	query = _apply_filters(query, S, params or ctx.params)
	if columns:
		query = query.groupby(*columns)
	return query.run(as_dict=True)


def _daily(ctx, start, end, group_by=("stat_date",)):
	"""Summed daily metrics for start <= stat_date <= end, grouped by `group_by`."""
	D = frappe.qb.DocType(rollup.DAILY)
	columns = [getattr(D, name) for name in group_by]
	query = (
		frappe.qb.from_(D)
		.select(*columns, *(Sum(getattr(D, m)).as_(m) for m in rollup.DAILY_METRICS))
		.where(D.stat_date >= start)
		.where(D.stat_date <= end)
	)
	query = _apply_filters(query, D, ctx.params)
	if columns:
		query = query.groupby(*columns)
	return query.run(as_dict=True)


def _total(rows, metric):
	# SUM() comes back as Decimal, which will not mix with the floats rates are made of.
	return float(sum(row.get(metric) or 0 for row in rows))


def _by_status(rows):
	counts = dict.fromkeys(STATUS_ORDER, 0)
	for row in rows:
		if row.status in counts:
			counts[row.status] += int(row.grievances or 0)
	return counts


def _pct(part, whole):
	return round(part * 100.0 / whole, 1) if whole else None


def _month_start(day):
	return day.replace(day=1)


def _week_start(day):
	return day - timedelta(days=day.weekday())


def _region_names():
	return dict(
		frappe.get_all(
			"Grievance Administrative Area",
			filters={"level_name": "Region"},
			fields=["code", "area_name"],
			as_list=True,
		)
	)


# Builders
# --------


def build_kpis(ctx):
	"""Total / Awaiting Action / Resolved / Escalated cards with their deltas."""
	by_status = _by_status(_snapshot(ctx, ("status",)))
	escalated = _total(
		[r for r in _snapshot(ctx, ("status", "escalated")) if r.escalated and r.status in C.OPEN_STATES],
		"grievances",
	)
	total = sum(by_status.values())

	month_start = _month_start(ctx.today)
	week_start = _week_start(ctx.today)
	this_month = _daily(ctx, month_start, ctx.today, group_by=())
	this_week = _daily(ctx, week_start, ctx.today, group_by=())

	# The total at the end of last month is today's total less what was filed since.
	previous_total = total - int(_total(this_month, "submitted_count"))
	delta_pct = round((total - previous_total) * 100.0 / previous_total, 1) if previous_total > 0 else None
	return [
		{"metric": "total", "value": total, "delta_pct": delta_pct, "compare": "prev_month"},
		{
			"metric": "awaiting_action",
			"value": sum(by_status[s] for s in C.AWAITING_ACTION_STATES),
			"delta": int(_total(this_week, "submitted_count")),
			"compare": "this_week_new",
		},
		{
			"metric": "resolved",
			"value": sum(by_status[s] for s in C.RESOLVED_STATES),
			"delta": int(_total(this_week, "resolved_count")),
			"compare": "this_week_resolved",
		},
		{"metric": "escalated", "value": int(escalated)},
	]


def _performance(ctx, start, end, snapshot_day):
	events = _daily(ctx, start, end, group_by=())
	resolved = _total(events, "resolved_count")
	submitted = _total(events, "submitted_count")
	feedback = _total(events, "feedback_count")
	by_status = _by_status(_snapshot(ctx, ("status",), on_date=snapshot_day)) if snapshot_day else None
	return {
		"avg_resolution_time": (
			round(float(_total(events, "resolution_hours_sum")) / resolved / 24, 1) if resolved else None
		),
		"resolution_rate": (
			_pct(sum(by_status[s] for s in C.RESOLVED_STATES), sum(by_status.values())) if by_status else None
		),
		"escalation_rate": _pct(_total(events, "escalated_count"), submitted),
		"satisfaction": _pct(_total(events, "feedback_satisfied_count"), feedback),
		"basis": int(feedback),
	}


def _snapshot_on_or_before(day):
	S = frappe.qb.DocType(rollup.SNAPSHOT)
	row = (
		frappe.qb.from_(S)
		.select(S.snapshot_date)
		.where(S.snapshot_date <= day)
		.orderby(S.snapshot_date, order=frappe.qb.desc)
		.limit(1)
		.run()
	)
	return row[0][0] if row else None


def build_performance_kpis(ctx):
	"""Avg Resolution Time / Resolution Rate / Escalation Rate / Satisfaction for a month."""
	month_start = getdate(f"{ctx.params.month}-01") if ctx.params.month else _month_start(ctx.today)
	month_end = min(month_start.replace(day=monthrange(month_start.year, month_start.month)[1]), ctx.today)
	prev_start = getdate(add_months(month_start, -1))
	prev_end = month_start - timedelta(days=1)

	current = _performance(ctx, month_start, month_end, _snapshot_on_or_before(month_end))
	# A previous month from before the rollups existed has no snapshot to compare with.
	prev_snapshot = _snapshot_on_or_before(prev_end)
	previous = _performance(
		ctx, prev_start, prev_end, prev_snapshot if prev_snapshot and prev_snapshot >= prev_start else None
	)

	def delta(metric):
		if current[metric] is None or previous[metric] is None:
			return None
		return round(current[metric] - previous[metric], 1)

	return [
		{
			"metric": "avg_resolution_time",
			"value": current["avg_resolution_time"],
			"unit": "days",
			"delta": delta("avg_resolution_time"),
		},
		{
			"metric": "resolution_rate",
			"value": current["resolution_rate"],
			"unit": "percent",
			"delta": delta("resolution_rate"),
		},
		{
			"metric": "escalation_rate",
			"value": current["escalation_rate"],
			"unit": "percent",
			"delta": delta("escalation_rate"),
		},
		{
			"metric": "satisfaction",
			"value": current["satisfaction"],
			"unit": "percent",
			"basis": current["basis"],
		},
	]


def _periods(start, end, granularity):
	"""The period keys from start to end inclusive, and a function from a day to its key."""
	if granularity == "week":
		first = _week_start(start)
		keys = []
		day = first
		while day <= end:
			keys.append(day.isoformat())
			day += timedelta(days=7)
		return keys, lambda d: _week_start(d).isoformat()
	keys = []
	day = _month_start(start)
	while day <= end:
		keys.append(day.strftime("%Y-%m"))
		day = getdate(add_months(day, 1))
	return keys, lambda d: d.strftime("%Y-%m")


def _bucketed(ctx, start, end, granularity, metrics):
	keys, key_of = _periods(start, end, granularity)
	buckets = {key: dict.fromkeys(metrics, 0) for key in keys}
	for row in _daily(ctx, start, end):
		bucket = buckets.get(key_of(getdate(row.stat_date)))
		if bucket is not None:
			for metric in metrics:
				bucket[metric] += int(row[metric] or 0)
	return keys, buckets


def build_monthly_trend(ctx):
	"""Filed and resolved per month; six months by default, empty months as zeros."""
	start = ctx.params.from_date or getdate(add_months(_month_start(ctx.today), -5))
	end = ctx.params.to_date or ctx.today
	keys, buckets = _bucketed(ctx, start, end, "month", ("submitted_count", "resolved_count"))
	return [
		{"month": k, "submitted": buckets[k]["submitted_count"], "resolved": buckets[k]["resolved_count"]}
		for k in keys
	]


def build_weekly_trend(ctx):
	"""Received and resolved per ISO week (Monday start); seven weeks by default."""
	start = ctx.params.from_date or (_week_start(ctx.today) - timedelta(weeks=6))
	end = ctx.params.to_date or ctx.today
	keys, buckets = _bucketed(ctx, start, end, "week", ("submitted_count", "resolved_count"))
	return [
		{"week_start": k, "received": buckets[k]["submitted_count"], "resolved": buckets[k]["resolved_count"]}
		for k in keys
	]


def build_net_backlog_trend(ctx):
	"""Filed, resolved and the open backlog at the end of each period.

	Worked backwards from today's open count: the backlog at the end of a period is
	today's, less everything filed since, plus everything resolved or rejected since.
	"""
	granularity = ctx.params.granularity
	end = ctx.today
	if ctx.params.from_date:
		start = ctx.params.from_date
	elif granularity == "week":
		start = _week_start(end) - timedelta(weeks=5)
	else:
		start = getdate(add_months(_month_start(end), -5))

	metrics = ("submitted_count", "resolved_count", "rejected_count")
	keys, buckets = _bucketed(ctx, start, end, granularity, metrics)
	open_now = sum(_by_status(_snapshot(ctx, ("status",)))[s] for s in C.OPEN_STATES)

	rows = []
	backlog = open_now
	for key in reversed(keys):
		bucket = buckets[key]
		rows.append(
			{
				"period": key,
				"submitted": bucket["submitted_count"],
				"resolved": bucket["resolved_count"],
				"backlog": max(backlog, 0),
			}
		)
		backlog -= bucket["submitted_count"] - bucket["resolved_count"] - bucket["rejected_count"]
	return list(reversed(rows))


def build_status_distribution(ctx):
	"""Every state but Draft, zeros included, in lifecycle order."""
	return [{"status": s, "count": n} for s, n in _by_status(_snapshot(ctx, ("status",))).items()]


def build_by_category(ctx):
	rows = _snapshot(ctx, ("service_category",))
	return sorted(
		({"category": r.service_category, "count": int(r.grievances or 0)} for r in rows if r.grievances),
		key=lambda r: (-r["count"], r["category"] or ""),
	)


def build_category_resolution(ctx):
	"""Filed and resolved per category in the window, resolved split by SLA outcome."""
	start = ctx.params.from_date or getdate(add_months(_month_start(ctx.today), -5))
	end = ctx.params.to_date or ctx.today
	rows = _daily(ctx, start, end, group_by=("service_category",))
	result = [
		{
			"category": r.service_category,
			"filed": int(r.submitted_count or 0),
			"resolved": int(r.resolved_count or 0),
			"resolved_on_time": int(r.resolved_on_time_count or 0),
			"resolved_breached": int(r.resolved_breached_count or 0),
		}
		for r in rows
	]
	return sorted(result, key=lambda r: (-r["filed"], r["category"] or ""))


def build_resolution_rate_by_region(ctx):
	names = _region_names()
	totals = {}
	for r in _snapshot(ctx, ("region", "status")):
		if not r.region:
			continue
		entry = totals.setdefault(r.region, {"resolved": 0, "total": 0})
		entry["total"] += int(r.grievances or 0)
		if r.status in C.RESOLVED_STATES:
			entry["resolved"] += int(r.grievances or 0)
	rows = [
		{
			"region_code": code,
			"region_name": names.get(code, code),
			"resolved": v["resolved"],
			"total": v["total"],
			"rate": _pct(v["resolved"], v["total"]),
		}
		for code, v in totals.items()
	]
	return sorted(rows, key=lambda r: (-r["total"], r["region_code"]))


def build_sla_risk(ctx):
	"""At risk (due within 24 hours), breached, and escalated; a case may be in several."""
	counts = {"at_risk": 0, "breached": 0, "escalated": 0}
	for r in _snapshot(ctx, ("status", "sla_state", "escalated")):
		if r.status not in C.OPEN_STATES:
			continue
		n = int(r.grievances or 0)
		if r.sla_state in ("at_risk", "breached"):
			counts[r.sla_state] += n
		if r.escalated:
			counts["escalated"] += n
	return [{"bucket": bucket, "count": count} for bucket, count in counts.items()]


def build_pending_duplicates(ctx):
	return [{"count": int(_total(_snapshot(ctx, ()), "dup_pending"))}]


def _age_days(created, now):
	return (now - get_datetime(created)).days


def build_oldest_open(ctx):
	"""The longest-open case that is not escalated: its age, and for an admin, which case."""
	now = now_datetime()
	if ctx.admin:
		rows = _live_grievances(ctx, statuses=C.OPEN_STATES, escalated=0, limit=1, order="asc")
		return [
			{
				"ticket_number": r.ticket_number,
				"title": _title(r.description),
				"status": r.status,
				"age_days": _age_days(r.creation, now),
				"created_at": to_tz_aware_iso(r.creation),
			}
			for r in rows
		]
	rows = _snapshot(ctx, ())
	oldest = rows[0].oldest_open if rows else None
	if not oldest:
		return []
	return [{"age_days": _age_days(oldest, now), "created_at": to_tz_aware_iso(oldest)}]


def build_recent(ctx):
	names = _region_names()
	rows = _live_grievances(ctx, limit=ctx.params.limit, order="desc")
	return [
		{
			"ticket_number": r.ticket_number,
			"title": _title(r.description),
			"status": r.status,
			"service_category": r.service_category,
			"submitter_display": None if r.is_anonymous else r.submitter_name,
			"area_name": r.area_name,
			"region_code": r.region_code,
			"region_name": names.get(r.region_code),
			"created_at": to_tz_aware_iso(r.creation),
		}
		for r in rows
	]


def _filter_options(ctx, dimension, key, names=None):
	# Options list every value, so they ignore the filters the caller has set.
	rows = _snapshot(ctx, (dimension,), params=Params())
	result = []
	for r in rows:
		value = r[dimension]
		if not value or not r.grievances:
			continue
		entry = {key: value, "grievances": int(r.grievances)}
		if names is not None:
			entry["region_name"] = names.get(value, value)
		result.append(entry)
	return sorted(result, key=lambda e: (-e["grievances"], e[key]))


def build_filter_regions(ctx):
	return _filter_options(ctx, "region", "region_code", names=_region_names())


def build_filter_categories(ctx):
	return _filter_options(ctx, "service_category", "category")


def build_filter_departments(ctx):
	return _filter_options(ctx, "assigned_dept", "department")


# Live reads (admin view only)
# ----------------------------


def _title(description):
	text = " ".join((description or "").split())
	return text if len(text) <= TITLE_LENGTH else text[: TITLE_LENGTH - 1].rstrip() + "…"


def _live_grievances(ctx, statuses=None, escalated=None, limit=10, order="desc"):
	"""A handful of cases straight from Grievance, newest or oldest first."""
	G = frappe.qb.DocType("Grievance")
	A = frappe.qb.DocType("Grievance Administrative Area")
	R = frappe.qb.DocType("Grievance Administrative Area").as_("region_area")
	params = ctx.params
	query = (
		frappe.qb.from_(G)
		.join(A)
		.on(A.name == G.administrative_area)
		.left_join(R)
		.on((R.level_name == "Region") & (A.lft >= R.lft) & (A.lft <= R.rgt))
		.select(
			G.ticket_number,
			G.description,
			G.status,
			G.service_category,
			G.submitter_name,
			G.is_anonymous,
			G.creation,
			A.area_name,
			R.code.as_("region_code"),
		)
		.where(G.status != C.STATE_DRAFT)
		.orderby(G.creation, order=frappe.qb.desc if order == "desc" else frappe.qb.asc)
		.limit(limit)
	)
	if statuses:
		query = query.where(G.status.isin(statuses))
	if escalated is not None:
		query = query.where(G.escalated == escalated)
	if params.region:
		query = query.where(R.code.isin(params.region))
	if params.service_category:
		query = query.where(G.service_category.isin(params.service_category))
	if params.assigned_dept:
		query = query.where(G.assigned_dept.isin(params.assigned_dept))
	return query.run(as_dict=True)


# Registry
# --------

CHARTS = {
	"grvKpis": Chart(build_kpis),
	"grvPerformanceKpis": Chart(build_performance_kpis, params=FILTERS | {"month"}),
	"grvMonthlyTrend": Chart(build_monthly_trend, params=PERIOD),
	"grvWeeklyTrend": Chart(build_weekly_trend, params=PERIOD),
	"grvNetBacklogTrend": Chart(build_net_backlog_trend, params=FILTERS | {"from_date", "granularity"}),
	"grvStatusDistribution": Chart(build_status_distribution),
	"grvByCategory": Chart(build_by_category),
	"grvCategoryResolution": Chart(build_category_resolution, params=PERIOD),
	"grvResolutionRateByRegion": Chart(build_resolution_rate_by_region),
	"grvSlaRisk": Chart(build_sla_risk),
	"grvPendingDuplicates": Chart(build_pending_duplicates),
	"grvOldestOpen": Chart(build_oldest_open, live_for_admin=True),
	"grvRecent": Chart(build_recent, params=FILTERS | {"limit"}, public=False, live_for_admin=True),
	"grvFilterRegions": Chart(build_filter_regions, params=frozenset()),
	"grvFilterCategories": Chart(build_filter_categories, params=frozenset()),
	"grvFilterDepartments": Chart(build_filter_departments, params=frozenset(), public=False),
}
PUBLIC_CHARTS = tuple(chart_id for chart_id, chart in CHARTS.items() if chart.public)


def get_chart(chart_id, params, admin):
	"""Rows and as_of for one chart, from cache when possible. Raises on failure."""
	chart = CHARTS[chart_id]
	live = admin and chart.live_for_admin
	refreshed = rollup.as_of()
	used = params.used_by(chart)

	fingerprint = json.dumps(
		{"used": used, "as_of": None if live else str(refreshed)}, sort_keys=True, default=str
	)
	key = "grievance:charts:{}:{}:{}".format(
		chart_id, "admin" if admin else "public", hashlib.sha256(fingerprint.encode()).hexdigest()
	)
	cached = frappe.cache.get_value(key)
	if cached is not None:
		return cached

	rows = chart.build(Context(params, admin))
	stamp = now_datetime() if live else refreshed
	result = {"rows": rows, "as_of": to_tz_aware_iso(stamp) if stamp else None}
	frappe.cache.set_value(key, result, expires_in_sec=LIVE_TTL if live else ROLLUP_TTL)
	return result
