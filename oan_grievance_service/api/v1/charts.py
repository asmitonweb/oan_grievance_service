"""Dashboard charts over the analytics rollups.

    GET /api/v1/charts?charts=<id,...>&<filters>     Grievance Admin; any chart, several per call
    GET /api/v1/charts/<chart_id>?<filters>          no login; public charts only, one per call

Both are answered by services.dashboard from the rollups the scheduler refreshes
every 15 minutes, never from Grievance on the request; see that module for what
each chart returns. The public form is the contract the OAN dashboards read
(`GET <base>/api/v1/charts/<id>` returning `{data: [...]}`), and serves counts
only.

Each public chart is its own literal route rather than one `<chart_id>` route: the
JWT middleware exempts guest routes by exact path, and an unknown or admin-only id
then never reaches this code at all.
"""

import re
from datetime import timedelta
from typing import Literal

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime
from oan_auth_service.api.router import prefixed
from oan_auth_service.api.utils import (
	check_rate_limit,
	error_response,
	handle_api_errors,
	parse_multi_value,
	require_role,
	success_response,
)
from pydantic import BaseModel, ConfigDict, Field

from oan_grievance_service.services import constants as C
from oan_grievance_service.services import dashboard

route = prefixed("/api/v1")

ADMIN_ROLES = [C.ROLE_ADMIN, "System Manager", "Administrator"]
MAX_CHARTS = 20
MAX_SPAN_DAYS = 731
LIMITS = {"region": 100, "service_category": 50, "assigned_dept": 100}
RATE_LIMIT = 120  # requests per minute, per user (admin) or per address (public)

# Parameters only the admin view takes. The public view ignores them rather than
# rejecting them, so a caller cannot tell an admin-only parameter from a typo.
ADMIN_ONLY_PARAMS = ("assigned_dept", "limit")


class InvalidParam(Exception):
	def __init__(self, field, message):
		super().__init__(message)
		self.field = field
		self.message = message


class ChartsQuery(BaseModel):
	model_config = ConfigDict(extra="ignore", populate_by_name=True)

	region: str | list[str] | None = None
	service_category: str | list[str] | None = None
	assigned_dept: str | list[str] | None = None
	from_date: str | None = Field(None, alias="from")
	to_date: str | None = Field(None, alias="to")
	month: str | None = None
	granularity: Literal["month", "week"] = "month"
	limit: int = Field(10, ge=1, le=50)


def _parse_date(field, value):
	if not value:
		return None
	if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
		raise InvalidParam(field, _("Expected a date as YYYY-MM-DD"))
	try:
		return getdate(value)
	except Exception:
		raise InvalidParam(field, _("Not a valid date")) from None


def _known(doctype, fieldname, values, field, extra_filters=None):
	if not values:
		return ()
	if len(values) > LIMITS[field]:
		raise InvalidParam(field, _("At most {0} values").format(LIMITS[field]))
	filters = {fieldname: ["in", values], **(extra_filters or {})}
	known = set(frappe.get_all(doctype, filters=filters, pluck=fieldname))
	unknown = [v for v in values if v not in known]
	if unknown:
		raise InvalidParam(field, _("Unknown value(s): {0}").format(", ".join(unknown)))
	return tuple(values)


def parse_params(raw, admin):
	"""Validate the query string into dashboard.Params. Raises InvalidParam or pydantic's error."""
	raw = {k: v for k, v in raw.items() if k not in ("cmd", "charts")}
	if not admin:
		for name in ADMIN_ONLY_PARAMS:
			raw.pop(name, None)
		# The OAN dashboards name this filter `category`.
		if "category" in raw and "service_category" not in raw:
			raw["service_category"] = raw["category"]
	query = ChartsQuery.model_validate(raw)

	today = getdate(now_datetime())
	from_date = _parse_date("from", query.from_date)
	to_date = _parse_date("to", query.to_date)
	if from_date and to_date and from_date > to_date:
		raise InvalidParam("from", _("from must not be after to"))
	if from_date and (to_date or today) - from_date > timedelta(days=MAX_SPAN_DAYS):
		raise InvalidParam("from", _("The period may span at most two years"))

	month = query.month
	if month:
		if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
			raise InvalidParam("month", _("Expected a month as YYYY-MM"))
		if month > today.strftime("%Y-%m"):
			raise InvalidParam("month", _("month must not be in the future"))

	return dashboard.Params(
		region=_known(
			"Grievance Administrative Area",
			"code",
			parse_multi_value(query.region),
			"region",
			{"level_name": "Region", "is_active": 1},
		),
		service_category=_known(
			"Grievance Service Category",
			"name",
			parse_multi_value(query.service_category),
			"service_category",
		),
		assigned_dept=_known(
			"Grievance Department", "name", parse_multi_value(query.assigned_dept), "assigned_dept"
		),
		from_date=from_date,
		to_date=to_date,
		month=month,
		granularity=query.granularity,
		limit=query.limit,
	)


def _echo(params, admin):
	echo = {
		"region": list(params.region),
		"service_category": list(params.service_category),
		"from": params.from_date.isoformat() if params.from_date else None,
		"to": (params.to_date or getdate(now_datetime())).isoformat(),
	}
	if admin:
		echo["assigned_dept"] = list(params.assigned_dept)
	return echo


def _invalid(e):
	frappe.response["http_status_code"] = 400
	return error_response(
		message=_("Validation failed"), code="VALIDATION_ERROR", details={e.field: e.message}
	)


@route("/charts", methods=("GET",), summary="Dashboard charts (Grievance Admin)")
@frappe.whitelist(methods=["GET"])
@handle_api_errors
@require_role(ADMIN_ROLES)
def get_charts(charts: str | None = None, **kwargs):
	"""Several charts in one round trip, each built and cached on its own.

	A chart that fails comes back as `data[<id>] = null` with its error in
	`meta.errors`, so one broken tile does not blank the dashboard.
	"""
	check_rate_limit(f"grievance_charts:user:{frappe.session.user}", RATE_LIMIT, 60)

	requested = parse_multi_value(charts) or list(dashboard.CHARTS)
	if len(requested) > MAX_CHARTS:
		frappe.response["http_status_code"] = 400
		return error_response(
			message=_("Validation failed"),
			code="VALIDATION_ERROR",
			details={"charts": _("At most {0} charts per request").format(MAX_CHARTS)},
		)
	unknown = [c for c in requested if c not in dashboard.CHARTS]
	if unknown:
		frappe.response["http_status_code"] = 404
		return error_response(
			message=_("Unknown chart(s)"), code="NOT_FOUND", details={"unknown_charts": unknown}
		)

	try:
		params = parse_params(kwargs, admin=True)
	except InvalidParam as e:
		return _invalid(e)

	data, as_of, errors = {}, {}, {}
	for chart_id in requested:
		try:
			result = dashboard.get_chart(chart_id, params, admin=True)
			data[chart_id] = result["rows"]
			as_of[chart_id] = result["as_of"]
		except Exception:
			frappe.log_error(title=f"Dashboard chart {chart_id} failed")
			data[chart_id] = None
			errors[chart_id] = _("This chart could not be built")

	return success_response(
		data=data,
		message=_("Charts fetched successfully"),
		meta={"as_of": as_of, "filters": _echo(params, admin=True), "errors": errors},
	)


def _public_chart(chart_id, raw):
	check_rate_limit(
		f"grievance_charts:ip:{getattr(frappe.local, 'request_ip', None) or 'unknown'}", RATE_LIMIT, 60
	)
	try:
		params = parse_params(raw, admin=False)
	except InvalidParam as e:
		return _invalid(e)

	result = dashboard.get_chart(chart_id, params, admin=False)
	return success_response(
		data=result["rows"],
		message=_("Chart fetched successfully"),
		meta={"as_of": result["as_of"], "filters": _echo(params, admin=False)},
	)


def _public_endpoint(chart_id):
	def get_public_chart(**kwargs):
		return _public_chart(chart_id, kwargs)

	get_public_chart.__name__ = get_public_chart.__qualname__ = f"get_public_chart_{chart_id}"
	return handle_api_errors(get_public_chart)


for _chart_id in dashboard.PUBLIC_CHARTS:
	route(
		f"/charts/{_chart_id}",
		methods=("GET",),
		allow_guest=True,
		summary=f"Public dashboard chart {_chart_id} (counts only)",
	)(_public_endpoint(_chart_id))
