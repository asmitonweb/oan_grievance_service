#!/usr/bin/env python3
"""
generate_openapi_spec.py

Dynamically discovers all declared REST routes across oan_grievance_service,
introspects request/response models, query parameters, security requirements,
and builds openapi_v1.yaml and openapi_v1.public.yaml.

Outputs:
  - openapi_v1.yaml: Engineering/Internal specification with vendor extensions
    (x-legacy-rpc-method, x-schema-confidence).
  - openapi_v1.public.yaml: Public/Gateway contract with vendor extensions stripped.

Usage:
  python3 openapi/generate_openapi_spec.py
"""

import importlib
import inspect
import re
import sys
from pathlib import Path
from typing import Any

import frappe
import yaml
from oan_auth_service.api.router import _exempt_paths, _rules
from pydantic import BaseModel
from werkzeug.routing import Rule

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
INTERNAL_SPEC_OUTPUT = SCRIPT_DIR / "openapi_v1.yaml"
PUBLIC_SPEC_OUTPUT = SCRIPT_DIR / "openapi_v1.public.yaml"


# ---------------------------------------------------------------------------
# Schema building helper functions
# ---------------------------------------------------------------------------
def S(**kw: Any) -> dict[str, Any]:
	return {"type": "string", **kw}


def I(**kw: Any) -> dict[str, Any]:  # noqa: E743
	return {"type": "integer", **kw}


def N(**kw: Any) -> dict[str, Any]:
	return {"type": "number", **kw}


def B(**kw: Any) -> dict[str, Any]:
	return {"type": "boolean", **kw}


def ARR(items: Any, **kw: Any) -> dict[str, Any]:
	return {"type": "array", "items": items, **kw}


def OBJ(
	props: dict[str, Any],
	required: list[str] | None = None,
	description: str | None = None,
	confidence: str | None = None,
	**kw: Any,
) -> dict[str, Any]:
	d: dict[str, Any] = {"type": "object", "properties": props, **kw}
	if required:
		d["required"] = required
	if description:
		d["description"] = description
	if confidence:
		d["x-schema-confidence"] = confidence
	return d


# The body of a route that streams a file rather than a JSON envelope.
BINARY = {"type": "string", "format": "binary"}


def REF(name: str) -> dict[str, str]:
	return {"$ref": f"#/components/schemas/{name}"}


# ---------------------------------------------------------------------------
# Components: Data Schemas
# ---------------------------------------------------------------------------
DATA_SCHEMAS: dict[str, Any] = {}


def data(name: str, schema: dict[str, Any]) -> str:
	DATA_SCHEMAS[name] = schema
	return name


# Standard Envelopes & Metadata
data(
	"ApiMeta",
	OBJ(
		{
			"api_version": S(example="v1", description="Semantic API version"),
			"status": S(example="current", description="Lifecycle status"),
		},
		required=["api_version"],
	),
)

data(
	"StandardErrorResponse",
	OBJ(
		{
			"status": S(example="error", enum=["error"]),
			"message": S(description="Human-readable error description"),
			"exception": S(nullable=True, description="Exception class name"),
			"errors": ARR(
				OBJ(
					{
						"field": S(nullable=True, description="Field causing the validation error"),
						"message": S(description="Error message for the specific field"),
					}
				),
				nullable=True,
				description="Structured validation errors if applicable",
			),
			"meta": REF("ApiMeta"),
			"request_id": S(format="uuid", nullable=True, description="Tracing correlation ID"),
		},
		required=["status", "message"],
		description="Standard error envelope returned on 4xx/5xx responses",
	),
)

data(
	"PaginationMeta",
	OBJ(
		{
			"page": I(example=1, description="Current page number"),
			"page_size": I(example=20, description="Items per page"),
			"total_count": I(example=142, description="Total matching records"),
			"total_pages": I(example=8, description="Total pages available"),
		},
		required=["page", "page_size", "total_count", "total_pages"],
		description="Pagination metadata block",
	),
)

# Health & Ping
data(
	"HealthData",
	OBJ(
		{
			"status": S(example="healthy"),
			"service": S(example="oan_grievance_service"),
			"api_version": S(example="v1"),
		},
		required=["status", "service", "api_version"],
		description="Service health status payload",
	),
)

data(
	"PingData",
	OBJ(
		{
			"ping": S(example="pong"),
			"service": S(example="oan_grievance_service"),
			"api_version": S(example="v1"),
		},
		required=["ping", "service", "api_version"],
		description="Service ping payload",
	),
)

# Submitter Options & Profile
data(
	"SubmitterTypeItem",
	OBJ(
		{
			"type_name": S(example="Individual Farmer"),
			"code": S(example="IND_FARMER"),
			"description": S(nullable=True),
		},
		required=["type_name"],
	),
)

data(
	"SubmissionTypeItem",
	OBJ(
		{
			"type_name": S(example="Mobile App"),
			"code": S(example="MOB_APP"),
			"description": S(nullable=True),
		},
		required=["type_name"],
	),
)

data(
	"OptionKeyValue",
	OBJ(
		{
			"value": S(example="Inputs"),
			"label": S(example="Agricultural Inputs"),
			"code": S(nullable=True),
		},
		required=["value", "label"],
	),
)

data(
	"PhoneExtensionItem",
	OBJ(
		{
			"country": S(example="Ethiopia"),
			"code": S(example="ET"),
			"isd": S(example="+251"),
		},
		required=["country", "code", "isd"],
	),
)

data(
	"SubmitterOptionsData",
	OBJ(
		{
			"submitter_types": ARR(REF("SubmitterTypeItem")),
			"submission_types": ARR(REF("SubmissionTypeItem")),
			"preferred_languages": ARR(OBJ({"code": S(), "label": S()}, required=["code", "label"])),
			"service_categories": ARR(REF("OptionKeyValue")),
			"grievance_types": ARR(REF("OptionKeyValue")),
			"phone_extensions": ARR(REF("PhoneExtensionItem"), nullable=True),
		},
		required=[
			"submitter_types",
			"submission_types",
			"preferred_languages",
			"service_categories",
			"grievance_types",
		],
		description="Public dropdown options and intake reference data",
	),
)

data(
	"SubmitterProfileData",
	OBJ(
		{
			"profile_id": S(description="Unique Grievance Submitter Profile document name"),
			"full_name": S(description="Full name of submitter or representative"),
			"type": S(description="Submitter Type e.g. Individual Farmer or Cooperative"),
			"role": S(example="Grievance Submitter"),
			"identity_scheme": S(nullable=True, enum=["fayda", "org", "phone", None]),
			"identity_value": S(nullable=True),
			"fayda_id": S(nullable=True),
			"registration_number": S(nullable=True),
			"contact_mobile": S(nullable=True),
			"country_code": S(example="+251", nullable=True),
			"phone_number": S(example="911887766", nullable=True),
			"contact_email": S(format="email", nullable=True),
			"preferred_language": S(example="en", nullable=True),
			"administrative_area": S(nullable=True),
			"administrative_unit": S(nullable=True),
			"active": I(enum=[0, 1]),
			"is_blocked": I(enum=[0, 1]),
		},
		required=["profile_id", "full_name", "type", "role"],
		description="Grievance submitter profile details",
	),
)

data(
	"SubmitterIdentityItem",
	OBJ(
		{"scheme": S(example="phone"), "value": S(example="+251911887766")},
		required=["scheme", "value"],
		description="Submitter deduplication identity key",
	),
)

data(
	"SubmitterRegisterResultData",
	OBJ(
		{
			"profile_id": S(description="Unique Grievance Submitter Profile document name"),
			"submitter_type": S(example="Individual Farmer"),
			"submitter_name": S(example="Abebe Bikila", nullable=True),
			"contact_mobile": S(example="+251911887766", nullable=True),
			"country_code": S(example="+251", nullable=True),
			"phone_number": S(example="911887766", nullable=True),
			"contact_email": S(format="email", nullable=True),
			"dedupe_key": S(nullable=True),
			"identities": ARR(REF("SubmitterIdentityItem")),
			"administrative_area": S(nullable=True),
			"administrative_unit": S(nullable=True),
			"active": B(),
			"is_blocked": B(),
			"blocked_reason": S(nullable=True),
		},
		required=["profile_id", "submitter_type", "active", "is_blocked"],
		description="Outcome of submitter profile registration",
	),
)

data(
	"SubmitterBlockResultData",
	OBJ(
		{
			"profile_id": S(description="Submitter profile ID"),
			"is_blocked": B(description="Whether the submitter profile is blocked"),
			"blocked_reason": S(nullable=True, description="Reason for blocking"),
			"active": B(description="Whether the submitter profile is active"),
		},
		required=["profile_id", "is_blocked", "active"],
		description="Outcome of submitter block or unblock operation",
	),
)

# Administrative Areas
data(
	"AdministrativeAreaItem",
	OBJ(
		{
			"area_id": S(example="region-ET14", description="Canonical area ID"),
			"area_name": S(example="Oromia"),
			"code": S(example="ET14", nullable=True),
			"path_code": S(example="ET.ET14", nullable=True),
			"level_name": S(
				example="Region", description="Administrative tier (e.g. Region, Zone, Woreda, Kebele)"
			),
			"parent_administrative_area": S(nullable=True),
			"is_group": I(enum=[0, 1]),
			"depth": I(example=1),
		},
		required=["area_id", "area_name", "level_name"],
		description="Administrative area hierarchy node",
	),
)

data(
	"AdministrativeAreasListData",
	OBJ(
		{
			"areas": ARR(REF("AdministrativeAreaItem")),
			"count": I(example=12),
			"parent": S(nullable=True),
			"level_name": S(nullable=True),
		},
		required=["areas", "count"],
		description="List of administrative area nodes for cascading dropdowns or search",
	),
)

data(
	"BreadcrumbItem",
	OBJ(
		{
			"area_id": S(),
			"area_name": S(),
			"code": S(nullable=True),
			"path_code": S(nullable=True),
			"level_name": S(),
			"depth": I(),
		},
		required=["area_id", "area_name", "level_name"],
	),
)

data(
	"AreaAncestorsData",
	OBJ(
		{
			"current": REF("AdministrativeAreaItem"),
			"breadcrumbs": ARR(REF("BreadcrumbItem")),
		},
		required=["breadcrumbs"],
		description="Ancestor hierarchy breadcrumbs from country root to the node",
	),
)

# Grievance Drafts
data(
	"DraftData",
	OBJ(
		{
			"name": S(description="Draft document name"),
			"ticket_number": S(nullable=True, description="Assigned ticket number if submitted"),
			"client_submission_uuid": S(description="Stable client-generated draft key"),
			"status": S(example="Draft"),
			"workflow_state": S(example="Draft"),
			"submission_channel": S(nullable=True),
			"submitter_type": S(nullable=True),
			"submitter_name": S(nullable=True),
			"contact_mobile": S(nullable=True),
			"country_code": S(example="+251", nullable=True),
			"phone_number": S(example="911887766", nullable=True),
			"phone": S(nullable=True),
			"contact_email": S(nullable=True),
			"administrative_area": S(nullable=True),
			"administrative_unit": S(nullable=True),
			"service_category": S(nullable=True),
			"grievance_type": S(nullable=True),
			"associated_service_provider": S(nullable=True),
			"description": S(nullable=True),
			"desired_outcome": S(nullable=True),
			"is_anonymous": I(enum=[0, 1]),
			"attachments": ARR(OBJ({})),
			"attachment_count": I(),
			"owner": S(nullable=True),
		},
		required=["client_submission_uuid", "status", "workflow_state"],
		description="Draft grievance state",
	),
)

data(
	"DraftDiscardData",
	OBJ(
		{
			"discarded": B(description="Whether the draft was successfully discarded"),
		},
		required=["discarded"],
		description="Outcome of draft discard operation",
	),
)

# Grievance Core & Lifecycle
data(
	"GrievanceSubmitResultData",
	OBJ(
		{
			"ticket_number": S(example="ET14IN000012026", description="Formatted ticket number"),
			"status": S(example="Submitted"),
			"acknowledgement_status": S(example="Sent", nullable=True),
			"assigned_officer": S(nullable=True),
			"sla_target_date": S(format="date-time", nullable=True),
			"creation": S(format="date-time"),
			"is_anonymous": I(enum=[0, 1], example=0),
			"workflow_state": S(nullable=True),
			"client_submission_uuid": S(nullable=True),
			"routing_rule": S(nullable=True),
		},
		required=["ticket_number", "status"],
		description="Acknowledgement outcome and ticket identifier returned on submission",
	),
)

data(
	"GrievanceListItem",
	OBJ(
		{
			"name": S(description="Internal document ID"),
			"ticket_number": S(example="3001002A0"),
			"ticket_number_display": S(
				example="3-001-002A-0", nullable=True, description="Grouped ticket number for human reading"
			),
			"status": S(
				example="Submitted",
				description="Public workflow status (Submitted, Under Investigation, Require More Info, Resolved, Closed, Reopened, Rejected)",
			),
			"service_category": S(example="Inputs"),
			"grievance_type": S(example="Fertilizer Shortage"),
			"administrative_area": S(example="kebele-ET140108101008"),
			"administrative_unit": S(nullable=True),
			"submitter_name": S(example="Abebe Bikila"),
			"contact_mobile": S(example="+251911887766", nullable=True),
			"country_code": S(example="+251", nullable=True),
			"phone_number": S(example="911887766", nullable=True),
			"contact_email": S(nullable=True),
			"assigned_officer": S(nullable=True),
			"sla_target_date": S(format="date-time", nullable=True),
			"is_escalated": I(enum=[0, 1]),
			"escalated": B(nullable=True),
			"is_anonymous": B(nullable=True),
			"confirmation_deadline": S(format="date-time", nullable=True),
			"creation": S(format="date-time"),
			"modified": S(format="date-time"),
		},
		required=["ticket_number", "status", "service_category", "grievance_type", "creation"],
		description="Summary record of a grievance in list view",
	),
)

data(
	"GrievanceListData",
	OBJ(
		{
			"grievances": ARR(REF("GrievanceListItem")),
			"pagination": REF("PaginationMeta"),
		},
		required=["grievances", "pagination"],
		description="Filtered and paginated list of grievances",
	),
)

data(
	"GrievanceDetailData",
	OBJ(
		{
			"ticket_number": S(example="3001002A0"),
			"ticket_number_display": S(
				example="3-001-002A-0", nullable=True, description="Grouped ticket number for human reading"
			),
			"name": S(),
			"status": S(),
			"service_category": S(),
			"grievance_type": S(),
			"description": S(),
			"submission_channel": S(),
			"preferred_language": S(nullable=True),
			"administrative_area": S(),
			"administrative_unit": S(nullable=True),
			"submitter": S(nullable=True),
			"submitter_name": S(),
			"contact_mobile": S(nullable=True),
			"country_code": S(example="+251", nullable=True),
			"phone_number": S(example="911887766", nullable=True),
			"contact_email": S(nullable=True),
			"is_anonymous": I(enum=[0, 1]),
			"assigned_officer": S(nullable=True),
			"assisted_by_officer": S(nullable=True),
			"sla_target_date": S(format="date-time", nullable=True),
			"sla_status": S(nullable=True),
			"resolution_details": S(nullable=True),
			"satisfaction_rating": I(nullable=True),
			"reopen_count": I(example=0),
			"is_escalated": I(enum=[0, 1]),
			"confirmation_deadline": S(format="date-time", nullable=True),
			"creation": S(format="date-time"),
			"modified": S(format="date-time"),
		},
		required=["ticket_number", "status", "description", "creation"],
		description="Full grievance case details",
	),
)

data(
	"TimelineEventItem",
	OBJ(
		{
			"name": S(nullable=True, description="Timeline entry identifier"),
			"entry_type": S(
				example="status_change",
				description="Event classification (status_change, note, message, attachment)",
			),
			"from_status": S(nullable=True),
			"to_status": S(nullable=True),
			"author_role": S(nullable=True, description="Role of the actor e.g. Woreda Officer or Submitter"),
			"author_type": S(nullable=True, enum=["submitter", "officer", "system"]),
			"body": S(nullable=True, description="Timeline message or description text"),
			"is_internal": B(description="Whether visible only to staff"),
			"created_on": S(format="date-time", nullable=True),
			"creation": S(format="date-time", nullable=True),
		},
		required=["entry_type"],
		description="Audit and communication event on the grievance timeline",
	),
)

data(
	"TimelineSubmitterDetail",
	OBJ(
		{
			"name": S(nullable=True),
			"mobile": S(nullable=True),
			"contact_mobile": S(nullable=True),
			"country_code": S(nullable=True),
			"phone_number": S(nullable=True),
			"email": S(nullable=True),
			"contact_email": S(nullable=True),
			"submitter_type": S(nullable=True),
			"is_anonymous": B(),
			"assisted_by_officer": S(nullable=True),
		},
		required=["is_anonymous"],
		description="Submitter contact and identity details on the timeline",
	),
)

data(
	"GrievanceTimelineData",
	OBJ(
		{
			"ticket_number": S(example="ET14IN000012026"),
			"status": S(example="Under Investigation"),
			"escalated": B(),
			"summary": OBJ({"description": S(nullable=True), "desired_outcome": S(nullable=True)}),
			"submitter": REF("TimelineSubmitterDetail"),
			"timeline": ARR(REF("TimelineEventItem")),
		},
		required=["ticket_number", "status", "timeline"],
		description="Chronological event log and message history",
	),
)

data(
	"GrievanceActionResultData",
	OBJ(
		{
			"ticket_number": S(example="ET14IN000012026"),
			"status": S(example="Under Investigation"),
			"message": S(description="Result confirmation message"),
			"action_timestamp": S(format="date-time", nullable=True),
		},
		required=["ticket_number", "status", "message"],
		description="Outcome of a state transition or action on a grievance",
	),
)

data(
	"GrievanceOptionsData",
	OBJ(
		{
			"statuses": ARR(
				OBJ(
					{
						"status": S(),
						"label": S(),
						"order": I(description="Display order of the queue status"),
						"is_open": I(),
						"is_terminal": I(
							description="1 when every mapped Frappe workflow state is terminal; 0 when absent"
						),
					}
				)
			),
			"departments": ARR(
				OBJ({"department_id": S(), "department_name": S()}, additionalProperties=True)
			),
			"service_categories": ARR(REF("OptionKeyValue")),
			"grievance_types": ARR(REF("OptionKeyValue")),
			"submission_channels": ARR(S()),
		},
		required=["statuses", "departments", "service_categories", "grievance_types", "submission_channels"],
		description="Grievance management options and active dropdown choices for staff",
	),
)

data(
	"StatusCard",
	OBJ(
		{
			"status": S(example="In Progress"),
			"label": S(example="In Progress"),
			"order": I(description="Display order of the queue status", example=2),
			"is_open": I(enum=[0, 1], example=1),
			"is_terminal": I(
				description="1 when every mapped Frappe workflow state is terminal. Absent workflow states are non-terminal.",
				enum=[0, 1],
				example=0,
			),
			"count": I(
				description="Grievances on this card visible to the caller. Omitted on the options list.",
				example=12,
			),
		},
		required=["status", "label", "order", "is_open", "is_terminal"],
		description="One queue status for the all-grievances KPI cards",
	),
)

data(
	"GrievanceStatusSummaryData",
	OBJ(
		{"cards": ARR(REF("StatusCard"))},
		required=["cards"],
		description="Status summary for the all-grievances queue. Draft is excluded.",
	),
)

# Attachments
data(
	"AttachmentItem",
	OBJ(
		{
			"attachment": S(description="Unique identifier of the attachment record"),
			"name": S(description="Document name (alias for attachment)", nullable=True),
			"file_name": S(description="Original filename"),
			"file_url": S(description="URL to the uploaded file", nullable=True),
			"mime_type": S(description="MIME type of the file"),
			"size_bytes": I(description="File size in bytes"),
			"checksum_sha256": S(description="SHA-256 checksum"),
			"scan_status": S(description="Antivirus scan status (Pending, Clean, Infected)"),
			"document_type": S(nullable=True, description="Classification of document"),
			"creation": S(format="date-time", nullable=True),
		},
		required=["file_name", "mime_type", "size_bytes"],
		description="Metadata for an uploaded evidence attachment",
	),
)

data(
	"AttachmentDownloadData",
	OBJ(
		{
			"file_name": S(description="Original filename"),
			"file_url": S(
				description="Frappe private-file URL; needs a Frappe session cookie, not a bearer token"
			),
			"view_url": S(
				description="API route that streams the bytes under the bearer token: "
				"/api/v1/attachments/{attachment_id}/view"
			),
			"mime_type": S(description="MIME type"),
			"size_bytes": I(description="Size in bytes"),
			"checksum_sha256": S(description="SHA-256 checksum"),
		},
		required=["file_name", "file_url", "view_url", "mime_type", "size_bytes"],
		description="Download metadata for a clean attachment",
	),
)

data(
	"DeleteAttachmentData",
	OBJ(
		{"deleted": B(description="True if attachment was successfully deleted")},
		required=["deleted"],
		description="Confirmation of attachment removal",
	),
)

# Change Requests
data(
	"ChangeItem",
	OBJ(
		{
			"fieldname": S(description="Grievance field to modify"),
			"old_value": S(nullable=True, description="Value before change"),
			"new_value": S(nullable=True, description="Requested value"),
		},
		required=["fieldname"],
		description="Specific field modification item in a change request",
	),
)

data(
	"ApprovalTrailItem",
	OBJ(
		{
			"action": S(example="Requested", description="Approval lifecycle action"),
			"user": S(description="User performing action"),
			"pending_with": S(nullable=True, description="User or role pending decision"),
			"at": S(format="date-time", nullable=True, description="Timestamp of action"),
			"note": S(nullable=True, description="Approval or rejection remarks"),
		},
		required=["action", "user"],
		description="Change request approval audit step",
	),
)

data(
	"ChangeRequestData",
	OBJ(
		{
			"name": S(description="Unique change request document ID"),
			"ticket_number": S(description="Associated grievance ticket number"),
			"subject": S(description="Summary of requested change"),
			"reason": S(nullable=True, description="Reason or justification"),
			"status": S(example="Pending", enum=["Pending", "Approved", "Rejected"]),
			"requested_by": S(description="User who raised the request"),
			"requested_at": S(format="date-time", nullable=True),
			"pending_with": S(nullable=True, description="User currently required to decide"),
			"pending_since": S(format="date-time", nullable=True),
			"decided_by": S(nullable=True, description="User who finalized decision"),
			"decided_at": S(format="date-time", nullable=True),
			"decision_note": S(nullable=True, description="Decision explanation"),
			"changes": ARR(REF("ChangeItem")),
			"trail": ARR(REF("ApprovalTrailItem")),
		},
		required=["name", "ticket_number", "subject", "status", "changes", "trail"],
		description="Change request snapshot and hierarchy approval status",
	),
)

data(
	"ChangeRequestListData",
	OBJ(
		{
			"items": ARR(REF("ChangeRequestData")),
			"count": I(example=3, description="Number of matching change requests"),
		},
		required=["items", "count"],
		description="List of change requests",
	),
)

data(
	"AvailableActionItem",
	OBJ(
		{
			"action": S(description="Action name"),
			"label": S(description="Localized action label"),
			"requires_reason": B(description="Whether a reason is mandatory"),
		},
		required=["action", "label", "requires_reason"],
	),
)

data(
	"GrievanceCurrentState",
	OBJ(
		{
			"status": S(description="Public grievance status"),
			"escalated": B(description="Whether case is currently escalated"),
			"assigned_to": S(nullable=True, description="Assigned officer email"),
			"department": S(nullable=True, description="Assigned department ID"),
			"updated_at": S(format="date-time", nullable=True),
			"available_actions": ARR(REF("AvailableActionItem")),
		},
		required=["status", "escalated", "available_actions"],
		description="Current case workflow status and permitted next actions",
	),
)

data(
	"GrievanceChangeResponseData",
	OBJ(
		{
			"ticket_number": S(example="ET14IN000012026", description="Grievance ticket number"),
			"status": S(example="Under Investigation"),
			"change_request": REF("ChangeRequestData"),
			"current_state": REF("GrievanceCurrentState"),
			"timeline_event": OBJ(
				{
					"name": S(nullable=True),
					"entry_type": S(),
					"body": S(nullable=True),
					"is_internal": B(),
					"author_role": S(),
					"author_type": S(),
					"from_status": S(nullable=True),
					"to_status": S(nullable=True),
					"created_on": S(format="date-time", nullable=True),
				},
				nullable=True,
				description="Generated timeline event if immediately approved",
			),
			"assigned_dept": S(nullable=True),
			"assigned_to": S(nullable=True),
			"service_category": S(nullable=True),
			"grievance_type": S(nullable=True),
			"sla_due_date": S(format="date-time", nullable=True),
			"is_anonymous": B(nullable=True),
			"anonymity_status": S(nullable=True),
		},
		required=["ticket_number", "status", "change_request", "current_state"],
		description="Outcome of a change-request-backed modification on a grievance",
	),
)


# ---------------------------------------------------------------------------
# Request Body Schemas
# ---------------------------------------------------------------------------
REQ: dict[str, Any] = {}

REQ["SaveDraftRequest"] = OBJ(
	{
		"client_submission_uuid": S(
			minLength=1, nullable=True, description="Stable client-generated draft key"
		),
		"client_uuid": S(nullable=True, description="Alias for client_submission_uuid"),
		"submission_channel": S(nullable=True, description="Submission channel"),
		"submitter_type": S(nullable=True, description="Submitter type"),
		"submitter_name": S(nullable=True, description="Submitter citizen name"),
		"contact_mobile": S(nullable=True, description="Contact mobile phone"),
		"country_code": S(nullable=True, description="Country phone code prefix (e.g. +251)"),
		"phone_number": S(nullable=True, description="National phone number"),
		"phone": S(nullable=True, description="Phone alias"),
		"contact_email": S(format="email", nullable=True, description="Contact email address"),
		"administrative_area": S(nullable=True, description="Administrative area ID or path_code"),
		"administrative_unit": S(nullable=True, description="Specific local landmark or unit"),
		"service_category": S(nullable=True, description="Service category name"),
		"grievance_type": S(nullable=True, description="Grievance type name"),
		"associated_service_provider": S(nullable=True, description="Associated service provider"),
		"description": S(nullable=True, description="Draft narrative description"),
		"desired_outcome": S(nullable=True, description="Desired resolution outcome"),
		"is_anonymous": I(enum=[0, 1], default=0, nullable=True, description="1 if anonymous"),
		"validate": B(default=False, description="If true, execute validation on the draft payload"),
	},
	required=[],
	description="Payload for saving or updating a grievance draft",
)

REQ["SubmitDocumentsRequest"] = OBJ(
	{
		"grievance": S(description="Grievance ticket number or document identifier"),
		"document_type": S(nullable=True, description="Document type or category"),
		"response": S(nullable=True, description="Associated formal response ID if applicable"),
	},
	required=["grievance"],
	description="Supporting document upload request",
)

REQ["SubmitDraftRequest"] = OBJ(
	{
		"client_submission_uuid": S(minLength=1, description="Stable client-generated draft key to submit"),
		"consent_given": I(enum=[0, 1], default=1, description="1 to record citizen consent"),
		"is_anonymous": I(enum=[0, 1], default=0, description="1 to request anonymity"),
		"anonymity_justification": S(nullable=True, description="Justification for anonymity"),
		"submission_channel": S(nullable=True, description="Submission channel"),
		"submitter_type": S(nullable=True, description="Submitter type"),
		"submitter_name": S(nullable=True, description="Submitter citizen name"),
		"contact_mobile": S(nullable=True, description="Contact mobile phone"),
		"country_code": S(nullable=True, description="Country phone code prefix (e.g. +251)"),
		"phone_number": S(nullable=True, description="National phone number"),
		"phone": S(nullable=True, description="Phone alias"),
		"contact_email": S(format="email", nullable=True, description="Contact email"),
		"administrative_area": S(nullable=True, description="Administrative area"),
		"administrative_unit": S(nullable=True, description="Administrative unit"),
		"service_category": S(nullable=True, description="Service category"),
		"grievance_type": S(nullable=True, description="Grievance type"),
		"associated_service_provider": S(nullable=True, description="Associated service provider"),
		"description": S(nullable=True, description="Narrative description"),
		"desired_outcome": S(nullable=True, description="Desired outcome"),
	},
	required=["client_submission_uuid"],
	description="Payload for submitting a saved grievance draft",
)

REQ["SubmitGrievanceRequest"] = OBJ(
	{
		"client_submission_uuid": S(
			minLength=1, nullable=True, description="Stable client-generated submission key"
		),
		"submitter_type": S(nullable=True, description="Submitter classification"),
		"submitter_name": S(nullable=True, description="Submitter name or organization"),
		"contact_mobile": S(nullable=True, description="Contact mobile phone"),
		"country_code": S(nullable=True, description="Country dialing prefix e.g. +251"),
		"phone_number": S(nullable=True, description="National phone number"),
		"phone": S(nullable=True, description="Phone alias"),
		"contact_email": S(format="email", nullable=True, description="Contact email address"),
		"submission_channel": S(nullable=True, description="Intake channel"),
		"administrative_area": S(nullable=True, description="Administrative area ID"),
		"administrative_unit": S(nullable=True, description="Administrative unit / woreda / branch"),
		"service_category": S(nullable=True, description="Service category name"),
		"grievance_type": S(nullable=True, description="Grievance type name"),
		"associated_service_provider": S(nullable=True, description="Associated service provider"),
		"description": S(minLength=1, description="Narrative description of grievance"),
		"desired_outcome": S(nullable=True, description="Desired resolution outcome"),
		"is_anonymous": I(enum=[0, 1], default=0, nullable=True, description="1 if requesting anonymity"),
		"anonymity_justification": S(nullable=True, description="Justification for anonymity request"),
		"consent_given": I(enum=[0, 1], default=1, nullable=True, description="1 to record citizen consent"),
	},
	required=["description"],
	description="Payload for lodging a grievance directly",
)

REQ["DiscardDraftRequest"] = OBJ(
	{
		"client_submission_uuid": S(minLength=1, description="Stable client-generated draft key to discard"),
	},
	required=["client_submission_uuid"],
	description="Payload for discarding an unsubmitted grievance draft",
)

REQ["PostMessageRequest"] = OBJ(
	{
		"message": S(minLength=1, description="Message text posted to the conversation thread"),
	},
	required=["message"],
	description="Message payload",
)

REQ["AddNoteRequest"] = OBJ(
	{
		"note": S(minLength=1, description="Internal or external note content"),
		"is_internal": B(default=True, description="Whether this note is hidden from citizens (staff-only)"),
	},
	required=["note"],
	description="Staff note payload",
)

REQ["GrievanceActionRequest"] = OBJ(
	{
		"action": S(
			minLength=1,
			description="Canonical workflow action name (e.g. 'Start Work', 'Confirm Resolution', 'Reopen', 'Reject')",
		),
		"reason": S(nullable=True, description="Mandatory justification when required by the action"),
		"note": S(nullable=True, description="Optional note text"),
		"rating": I(
			minimum=1,
			maximum=5,
			nullable=True,
			description="Citizen satisfaction rating (1-5) for resolution confirmation",
		),
		"comments": S(nullable=True, description="Optional citizen feedback remarks"),
		"body": S(nullable=True, description="Response body text for submitter reply"),
	},
	required=["action"],
	description="Workflow action and state transition payload",
)

REQ["DecideChangeRequest"] = OBJ(
	{
		"decision": S(minLength=1, enum=["Approved", "Rejected"], description="Approval decision"),
		"note": S(nullable=True, description="Decision note or rejection explanation"),
	},
	required=["decision"],
	description="Payload for approving or rejecting a change request",
)

REQ["ReassignGrievanceRequest"] = OBJ(
	{
		"target_department": S(minLength=1, description="Target department identifier"),
		"target_officer": S(nullable=True, description="Target officer user email"),
		"target_category": S(nullable=True, description="Target service category name"),
		"target_grievance_type": S(nullable=True, description="Target grievance type name"),
		"reason": S(nullable=True, description="Reassignment rationale"),
	},
	required=["target_department"],
	description="Payload for reassigning department or officer on a grievance",
)

REQ["DeferSLARequest"] = OBJ(
	{
		"additional_days": I(minimum=1, description="Number of additional days requested"),
		"reason": S(minLength=1, description="Justification for extending SLA deadline"),
	},
	required=["additional_days", "reason"],
	description="Payload for requesting an SLA deadline deferral",
)

REQ["AnonymityDecisionRequest"] = OBJ(
	{
		"decision": S(minLength=1, enum=["Approved", "Rejected"], description="Approval decision"),
		"reason": S(nullable=True, description="Mandatory reason when declining anonymity"),
	},
	required=["decision"],
	description="Payload for ruling on submitter anonymity request",
)

REQ["RegisterSubmitterRequest"] = OBJ(
	{
		"user": S(nullable=True, description="Existing user ID to associate"),
		"submitter_type": S(default="Individual Farmer", description="Submitter classification"),
		"submitter_name": S(nullable=True, description="Submitter full name or organization name"),
		"contact_mobile": S(nullable=True, description="Contact mobile number"),
		"country_code": S(nullable=True, description="Country dialing prefix"),
		"phone_number": S(nullable=True, description="National phone number"),
		"phone": S(nullable=True, description="Phone alias"),
		"contact_email": S(format="email", nullable=True, description="Contact email address"),
		"administrative_area": S(nullable=True, description="Administrative area link"),
		"administrative_unit": S(nullable=True, description="Administrative unit / woreda / branch"),
		"preferred_language": S(nullable=True, description="Preferred language code"),
		"fayda_id": S(nullable=True, description="National Fayda ID"),
		"national_id": S(nullable=True, description="National ID alias"),
		"registration_number": S(nullable=True, description="Organization registration number"),
		"org_number": S(nullable=True, description="Organization number alias"),
		"farmer_id": S(nullable=True, description="Farmer registry ID"),
		"dedupe_key": S(nullable=True, description="Explicit deduplication key"),
	},
	required=[],
	description="Payload for registering or updating a submitter profile",
)

REQ["BlockSubmitterRequest"] = OBJ(
	{
		"reason": S(minLength=1, description="Reason for blocking submitter"),
	},
	required=["reason"],
	description="Payload for blocking a submitter profile",
)


# Dashboard charts. Rows differ per chart and are documented on each route; each is
# a flat object of counts, codes and labels, never case detail on the public routes.
data(
	"DashboardChartRow",
	OBJ({}, additionalProperties=True, description="One row of a chart; the keys depend on the chart"),
)
data(
	"DashboardChartsData",
	OBJ(
		{},
		additionalProperties={**ARR(REF("DashboardChartRow")), "nullable": True},
		description="Rows per requested chart id; null for a chart that failed (see meta.errors)",
	),
)


# ---------------------------------------------------------------------------
# Envelope Builder Helper
# ---------------------------------------------------------------------------
def make_envelope(
	data_ref: str,
	is_list: bool = False,
	nullable_data: bool = False,
	description: str = "Successful response",
) -> dict[str, Any]:
	if is_list:
		data_prop: Any = ARR(REF(data_ref))
	elif nullable_data:
		data_prop = {**REF(data_ref), "nullable": True}
	else:
		data_prop = REF(data_ref)

	return OBJ(
		{
			"status": S(example="success", enum=["success"]),
			"message": S(nullable=True, description="Optional response message"),
			"data": data_prop,
			"meta": REF("ApiMeta"),
			"request_id": S(format="uuid", nullable=True, description="Tracing correlation ID"),
		},
		required=["status", "data"],
		description=description,
	)


ENVELOPES = {
	"HealthResponse": make_envelope("HealthData", description="Health check response"),
	"PingResponse": make_envelope("PingData", description="Ping response"),
	"SubmitterOptionsResponse": make_envelope(
		"SubmitterOptionsData", description="Submitter options response"
	),
	"SubmitterProfileResponse": make_envelope(
		"SubmitterProfileData", description="Submitter profile response"
	),
	"SubmitterRegisterResponse": make_envelope(
		"SubmitterRegisterResultData", description="Submitter registration response"
	),
	"SubmitterBlockResponse": make_envelope(
		"SubmitterBlockResultData", description="Submitter block/unblock response"
	),
	"AdministrativeAreasListResponse": make_envelope(
		"AdministrativeAreasListData", description="Administrative areas list response"
	),
	"AreaAncestorsResponse": make_envelope("AreaAncestorsData", description="Area ancestors response"),
	"DraftResponse": make_envelope("DraftData", nullable_data=True, description="Grievance draft response"),
	"DraftSubmitResultResponse": make_envelope(
		"GrievanceSubmitResultData", description="Grievance draft submission outcome response"
	),
	"DraftDiscardResponse": make_envelope("DraftDiscardData", description="Draft discard outcome response"),
	"GrievanceSubmitResultResponse": make_envelope(
		"GrievanceSubmitResultData", description="Grievance submission outcome response"
	),
	"GrievanceListResponse": make_envelope(
		"GrievanceListData", description="Paginated grievance list response"
	),
	"GrievanceDetailResponse": make_envelope("GrievanceDetailData", description="Grievance details response"),
	"GrievanceOptionsResponse": make_envelope(
		"GrievanceOptionsData", description="Grievance options response"
	),
	"DashboardChartResponse": make_envelope(
		"DashboardChartRow",
		is_list=True,
		description="Rows of one public dashboard chart; meta.as_of is the rollup time",
	),
	"DashboardChartsResponse": make_envelope(
		"DashboardChartsData", description="Rows per chart; meta.as_of and meta.errors per chart"
	),
	"GrievanceStatusSummaryResponse": make_envelope(
		"GrievanceStatusSummaryData", description="KPI status card counts"
	),
	"GrievanceTimelineResponse": make_envelope(
		"GrievanceTimelineData", description="Grievance timeline response"
	),
	"GrievanceActionResultResponse": make_envelope(
		"GrievanceActionResultData", description="Action result response"
	),
	"ChangeRequestResponse": make_envelope(
		"ChangeRequestData", description="Grievance change request response"
	),
	"ChangeRequestListResponse": make_envelope(
		"ChangeRequestListData", description="List of change requests response"
	),
	"GrievanceChangeResponse": make_envelope(
		"GrievanceChangeResponseData", description="Grievance change action response"
	),
	"AttachmentUploadResponse": make_envelope(
		"AttachmentItem", is_list=True, description="Attachment upload response"
	),
	"AttachmentListResponse": make_envelope(
		"AttachmentItem", is_list=True, description="Attachment list response"
	),
	"AttachmentDownloadResponse": make_envelope(
		"AttachmentDownloadData", description="Attachment download URL response"
	),
	"DeleteAttachmentResponse": make_envelope(
		"DeleteAttachmentData", description="Attachment deletion response"
	),
}


# ---------------------------------------------------------------------------
# Query Parameters Catalog
# ---------------------------------------------------------------------------
QP: dict[str, list[dict[str, Any]]] = {
	"SubmitterOptions": [
		{
			"name": "search_country",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Filter phone ISD prefixes",
		},
		{
			"name": "country",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Exact country name or 2-letter ISO code",
		},
		{
			"name": "include_phone_extensions",
			"in": "query",
			"required": False,
			"schema": B(default=True),
			"description": "Include phone dialing codes",
		},
		{
			"name": "service_category",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Filter grievance types by category",
		},
	],
	"AdministrativeAreas": [
		{
			"name": "parent",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Parent area ID, path_code, or comma-separated list of parents (cascading drill-down)",
		},
		{
			"name": "level_name",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Filter by administrative tier (e.g. Region, Zone, Woreda, Kebele)",
		},
		{
			"name": "search",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Free text search by area name or code",
		},
		{
			"name": "ancestors_of",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Return breadcrumb chain for specified node",
		},
		{
			"name": "limit",
			"in": "query",
			"required": False,
			"schema": I(default=100, maximum=500),
			"description": "Maximum records returned",
		},
	],
	"ListGrievances": [
		{
			"name": "page",
			"in": "query",
			"required": False,
			"schema": I(default=1, minimum=1),
			"description": "Page number",
		},
		{
			"name": "page_size",
			"in": "query",
			"required": False,
			"schema": I(default=20, minimum=1, maximum=100),
			"description": "Page size",
		},
		{
			"name": "status",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": (
				"Comma-separated queue statuses: All, In Progress, Require More Info, "
				+ "Rejected, Resolved, Closed. Draft is excluded."
			),
		},
		{
			"name": "category",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Comma-separated service categories e.g. 'Inputs,Payments'",
		},
		{
			"name": "service_category",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Single category alias",
		},
		{
			"name": "grievance_type",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Comma-separated grievance types",
		},
		{
			"name": "region",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Comma-separated regional administrative area IDs",
		},
		{
			"name": "administrative_area",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Single administrative area ID or path_code",
		},
		{
			"name": "from_date",
			"in": "query",
			"required": False,
			"schema": S(format="date"),
			"description": "Creation date lower bound (YYYY-MM-DD)",
		},
		{
			"name": "to_date",
			"in": "query",
			"required": False,
			"schema": S(format="date"),
			"description": "Creation date upper bound (YYYY-MM-DD)",
		},
		{
			"name": "search",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Free text search matching ticket, citizen name, or phone",
		},
		{
			"name": "sort_by",
			"in": "query",
			"required": False,
			"schema": S(default="creation"),
			"description": "Column to order by",
		},
		{
			"name": "sort_order",
			"in": "query",
			"required": False,
			"schema": S(default="asc", enum=["asc", "desc"]),
			"description": "Sort direction",
		},
	],
	"ListChangeRequests": [
		{
			"name": "status",
			"in": "query",
			"required": False,
			"schema": S(default="Pending", enum=["Pending", "Approved", "Rejected"]),
			"description": "Filter by change request status",
		},
		{
			"name": "scope",
			"in": "query",
			"required": False,
			"schema": S(default="pending_with_me", enum=["pending_with_me", "raised_by_me", "all"]),
			"description": "Filter scope by caller involvement",
		},
		{
			"name": "ticket_number",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Filter changes for a specific grievance ticket",
		},
		{
			"name": "limit",
			"in": "query",
			"required": False,
			"schema": I(default=50, maximum=200),
			"description": "Maximum records returned",
		},
	],
	"DashboardChart": [
		{
			"name": "region",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Comma-separated Region P-codes (e.g. ET04)",
		},
		{
			"name": "service_category",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Comma-separated service category names; `category` is accepted as an alias",
		},
		{
			"name": "from",
			"in": "query",
			"required": False,
			"schema": S(format="date"),
			"description": "Period start (trend and category-resolution charts)",
		},
		{
			"name": "to",
			"in": "query",
			"required": False,
			"schema": S(format="date"),
			"description": "Period end, default today",
		},
		{
			"name": "month",
			"in": "query",
			"required": False,
			"schema": S(pattern=r"^\d{4}-\d{2}$"),
			"description": "YYYY-MM for grvPerformanceKpis, default the current month",
		},
		{
			"name": "granularity",
			"in": "query",
			"required": False,
			"schema": S(enum=["month", "week"], default="month"),
			"description": "Period size for grvNetBacklogTrend",
		},
	],
	"ViewAttachment": [
		{
			"name": "download",
			"in": "query",
			"required": False,
			"schema": B(default=False),
			"description": "Send Content-Disposition: attachment (Save As) instead of inline",
		}
	],
}

# The admin form takes several charts at once and two filters the public one does not.
QP["DashboardCharts"] = [
	{
		"name": "charts",
		"in": "query",
		"required": False,
		"schema": S(),
		"description": "Comma-separated chart ids (at most 20); all charts when omitted",
	},
	*QP["DashboardChart"],
	{
		"name": "assigned_dept",
		"in": "query",
		"required": False,
		"schema": S(),
		"description": "Comma-separated department names",
	},
	{
		"name": "limit",
		"in": "query",
		"required": False,
		"schema": I(minimum=1, maximum=50, default=10),
		"description": "Rows for grvRecent",
	},
]


# ---------------------------------------------------------------------------
# Dynamic Discovery & Spec Builder
# ---------------------------------------------------------------------------
def _import_all_api_modules() -> None:
	"""Import all endpoint modules so Werkzeug rules are fully populated."""
	api_modules = [
		"oan_grievance_service.api.router",
		"oan_grievance_service.api.v1.administrative_area",
		"oan_grievance_service.api.v1.attachment",
		"oan_grievance_service.api.v1.change_request",
		"oan_grievance_service.api.v1.charts",
		"oan_grievance_service.api.v1.draft",
		"oan_grievance_service.api.v1.grievance",
		"oan_grievance_service.api.v1.profile",
		"oan_grievance_service.api.v1.submitter",
	]
	for mod_name in api_modules:
		try:
			importlib.import_module(mod_name)
		except Exception as e:
			print(f"Warning: Failed to import {mod_name}: {e}", file=sys.stderr)


def _determine_tag(path: str, func_name: str) -> str:
	if "/health" in path or "/ping" in path:
		return "Health & Monitoring"
	if path.startswith("/api/v1/charts"):
		return "Dashboard Charts"
	if path.startswith("/api/v1/submitters"):
		return "Submitter Management"
	if path.startswith("/api/v1/administrative-areas"):
		return "Administrative Areas"
	if path.startswith("/api/v1/drafts"):
		return "Grievance Drafts"
	if "/change-requests" in path or path.startswith("/api/v1/change-requests"):
		return "Change Requests"
	if "/attachments" in path or path.startswith("/api/v1/attachments"):
		return "Attachments"
	if any(
		kw in path
		for kw in ("action", "note", "message", "timeline", "reassign", "defer-sla", "anonymity-decision")
	):
		return "Grievance Lifecycle & Actions"
	return "Grievances Core"


def _determine_response(func_name: str, path: str, method: str) -> str | None:
	if func_name.startswith("get_public_chart_"):
		return "DashboardChartResponse"
	mapping = {
		"get_health": "HealthResponse",
		"get_ping": "PingResponse",
		"get_areas": "AdministrativeAreasListResponse",
		"get_area_ancestors": "AreaAncestorsResponse",
		"list_grievances": "GrievanceListResponse",
		"submit": "GrievanceSubmitResultResponse",
		"action": "GrievanceActionResultResponse",
		"timeline": "GrievanceTimelineResponse",
		"add_note": "GrievanceActionResultResponse",
		"message": "GrievanceActionResultResponse",
		"reassign": "GrievanceChangeResponse",
		"defer_sla": "GrievanceChangeResponse",
		"anonymity_decision": "GrievanceChangeResponse",
		"summary": "GrievanceStatusSummaryResponse",
		"get_charts": "DashboardChartsResponse",
		"options": "SubmitterOptionsResponse" if "submitters" in path else "GrievanceOptionsResponse",
		"me": "SubmitterProfileResponse",
		"submit_documents": "AttachmentUploadResponse",
		"get_attachments": "AttachmentListResponse",
		"download": "AttachmentDownloadResponse",
		"view": None,  # Binary stream
		"delete": "DeleteAttachmentResponse",
		"list_requests": "ChangeRequestListResponse",
		"get_request": "ChangeRequestResponse",
		"decide": "ChangeRequestResponse",
		"save": "DraftResponse",
		"load": "DraftResponse",
		"submit_draft": "DraftSubmitResultResponse",
		"discard": "DraftDiscardResponse",
		"register_submitter": "SubmitterRegisterResponse",
		"block_submitter": "SubmitterBlockResponse",
		"unblock_submitter": "SubmitterBlockResponse",
	}
	return mapping.get(func_name, "GrievanceActionResultResponse" if method == "POST" else None)


def build_openapi() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
	_import_all_api_modules()

	paths: dict[str, Any] = {}
	seen_ops: set[tuple[str, str]] = set()

	for rule in _rules:
		# Convert Werkzeug pattern e.g. <path:area_id_or_path> to {area_id_or_path}
		openapi_path = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", rule.rule)
		path_param_names = re.findall(r"<(?:\w+:)?(\w+)>", rule.rule)

		methods = [m.upper() for m in rule.methods if m.upper() != "HEAD"]
		endpoint_fn = rule.endpoint
		unwrapped = inspect.unwrap(endpoint_fn)
		func_name = unwrapped.__name__
		legacy_target = f"{unwrapped.__module__}.{func_name}"

		route_info = getattr(endpoint_fn, "_route", {})
		allow_guest = route_info.get("allow_guest", False) or rule.rule in _exempt_paths
		summary = route_info.get("summary") or (unwrapped.__doc__ or "").strip().split("\n")[0]
		description = (unwrapped.__doc__ or "").strip() or summary

		tag = _determine_tag(openapi_path, func_name)
		req_schema_cls = getattr(endpoint_fn, "_request_schema", None)
		req_schema_name = req_schema_cls.__name__ if req_schema_cls else None

		for method in sorted(methods):
			op_key = (method, openapi_path)
			if op_key in seen_ops:
				continue
			seen_ops.add(op_key)

			if openapi_path not in paths:
				paths[openapi_path] = {}

			# Path parameters
			parameters: list[dict[str, Any]] = []
			for p in path_param_names:
				param_desc = f"{p.replace('_', ' ').title()} identifier"
				if p == "ticket_number":
					param_desc = "Unique grievance ticket number (e.g. 3-001-002A-0 or 3001002A0)"
				elif p == "attachment":
					param_desc = "Unique attachment record ID"
				elif p == "area_id_or_path":
					param_desc = (
						"Area ID (e.g. 'kebele-ET140108101008') or path_code ('ET.ET14.01.08.101.008')"
					)

				parameters.append(
					{
						"name": p,
						"in": "path",
						"required": True,
						"schema": S(),
						"description": param_desc,
					}
				)

			# Query parameters
			if method == "GET":
				if func_name == "list_grievances":
					parameters.extend(QP["ListGrievances"])
				elif func_name == "get_areas":
					parameters.extend(QP["AdministrativeAreas"])
				elif func_name == "options" and "submitters" in openapi_path:
					parameters.extend(QP["SubmitterOptions"])
				elif func_name == "list_requests":
					parameters.extend(QP["ListChangeRequests"])
				elif func_name == "view":
					parameters.extend(QP["ViewAttachment"])
				elif func_name == "get_charts":
					parameters.extend(QP["DashboardCharts"])
				elif func_name.startswith("get_public_chart_"):
					parameters.extend(QP["DashboardChart"])

			response_schema_name = _determine_response(func_name, openapi_path, method)
			resp_content_type = "*/*" if func_name == "view" else "application/json"

			op: dict[str, Any] = {
				"tags": [tag],
				"summary": summary,
				"description": description,
				"operationId": f"{method.lower()}_{openapi_path.strip('/').replace('/', '_').replace('-', '_').replace('{', '').replace('}', '')}",
				"responses": {
					"200": {
						"description": "Success",
						"content": {
							resp_content_type: {
								"schema": REF(response_schema_name) if response_schema_name else BINARY
							}
						},
					},
					"400": {
						"description": "Validation or Bad Input Error",
						"content": {"application/json": {"schema": REF("StandardErrorResponse")}},
					},
					"401": {
						"description": "Unauthorized / Authentication Required",
						"content": {"application/json": {"schema": REF("StandardErrorResponse")}},
					},
					"403": {
						"description": "Forbidden / Insufficient Role Scope",
						"content": {"application/json": {"schema": REF("StandardErrorResponse")}},
					},
					"404": {
						"description": "Resource Not Found",
						"content": {"application/json": {"schema": REF("StandardErrorResponse")}},
					},
					"500": {
						"description": "Internal Server Error",
						"content": {"application/json": {"schema": REF("StandardErrorResponse")}},
					},
				},
			}

			if parameters:
				op["parameters"] = parameters

			op["x-legacy-rpc-method"] = legacy_target
			# The public chart routes are guest to the platform, but the gateway asks the
			# OAN dashboards for their key (DashboardKeyAuth) once it enforces auth.
			if func_name.startswith("get_public_chart_"):
				op["security"] = [{"DashboardKeyAuth": []}]
			else:
				op["security"] = [] if allow_guest else [{"BearerAuth": []}]

			# Request body for mutation methods
			if method in ("POST", "PUT", "PATCH", "DELETE") and req_schema_name:
				req_ct = (
					"multipart/form-data"
					if req_schema_name == "SubmitDocumentsRequest"
					else "application/json"
				)
				op["requestBody"] = {
					"required": True,
					"content": {req_ct: {"schema": REF(req_schema_name)}},
				}

			paths[openapi_path][method.lower()] = op

	components_schemas: dict[str, Any] = {}
	components_schemas.update(DATA_SCHEMAS)
	components_schemas.update(REQ)
	components_schemas.update(ENVELOPES)

	doc: dict[str, Any] = {
		"openapi": "3.0.3",
		"info": {
			"title": "OAN Grievance Service API",
			"version": "1.0.0",
			"description": (
				"Grievance management and citizen feedback service for OpenAgriNet (OAN). "
				+ "Provides RESTful endpoints for submitting complaints, tracking resolution progress, "
				+ "cascading administrative area drill-downs, citizen-officer timeline messaging, "
				+ "escalation management, change requests, and case resolution workflows."
			),
			"contact": {"name": "COSS - Centre for Open Societal Systems"},
		},
		"servers": [
			{"url": "http://localhost:8000", "description": "Local Frappe Bench"},
			{"url": "https://grievance.openagrinet.org", "description": "Production Grievance Gateway"},
		],
		"tags": [
			{"name": "Health & Monitoring", "description": "Service health probes and uptime pings"},
			{
				"name": "Submitter Management",
				"description": "Intake reference options, submitter registration, and moderation",
			},
			{
				"name": "Administrative Areas",
				"description": "Cascading geographic drill-downs, breadcrumbs, and search",
			},
			{
				"name": "Grievance Drafts",
				"description": "Draft grievance persistence, resume, submit, and discard",
			},
			{"name": "Grievances Core", "description": "Case intake, tracking, and filtered list views"},
			{
				"name": "Change Requests",
				"description": "Request, review, and decide on grievance field changes (department, officer, SLA deferral, anonymity)",
			},
			{
				"name": "Grievance Lifecycle & Actions",
				"description": "Communication threads, notes, reopen, reject, escalate, and resolution confirmation",
			},
			{
				"name": "Attachments",
				"description": "Supporting document and evidence upload, listing, download, and deletion",
			},
		],
		"paths": paths,
		"components": {
			"securitySchemes": {
				"BearerAuth": {
					"type": "http",
					"scheme": "bearer",
					"bearerFormat": "JWT",
					"description": "Provide JWT access token as `Bearer <token>` in the Authorization header.",
				},
				"DashboardKeyAuth": {
					"type": "apiKey",
					"in": "header",
					"name": "apikey",
					"description": "API key of the OAN dashboards (Kong consumer `oan-dashboards`, group `dashboards`). Checked and stripped by the gateway; the platform itself treats the chart routes as public, so before the gateway enforces keys the header is simply ignored.",
				},
			},
			"schemas": components_schemas,
		},
	}
	return doc, paths, components_schemas


def strip_extensions(o: Any) -> Any:
	if isinstance(o, dict):
		return {k: strip_extensions(v) for k, v in o.items() if not k.startswith("x-")}
	if isinstance(o, list):
		return [strip_extensions(v) for v in o]
	return o


def main() -> None:
	doc, paths, components_schemas = build_openapi()

	# 1. Write internal spec
	with open(INTERNAL_SPEC_OUTPUT, "w") as f:
		f.write("# OAN Grievance Service API -- OpenAPI 3.0.3 (INTERNAL)\n")
		f.write("# Carries internal vendor extensions (x-legacy-rpc-method).\n")
		f.write("# Dynamically generated from generate_openapi_spec.py -- do not edit manually.\n")
		yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False, width=100, allow_unicode=True)

	n_paths = len(paths)
	n_ops = sum(len(v) for v in paths.values())
	print(
		f"Wrote {INTERNAL_SPEC_OUTPUT.name}: {n_paths} paths, {n_ops} operations, {len(components_schemas)} schemas",
		file=sys.stderr,
	)

	# 2. Write public spec (vendor extensions stripped)
	public_doc = strip_extensions(doc)
	with open(PUBLIC_SPEC_OUTPUT, "w") as f:
		f.write("# OAN Grievance Service API -- OpenAPI 3.0.3 (PUBLIC)\n")
		f.write(
			"# Contract with vendor extensions removed. Dynamically generated from generate_openapi_spec.py.\n"
		)
		yaml.safe_dump(
			public_doc, f, sort_keys=False, default_flow_style=False, width=100, allow_unicode=True
		)

	print(
		f"Wrote {PUBLIC_SPEC_OUTPUT.name}: {n_paths} paths, {n_ops} operations, {len(components_schemas)} schemas",
		file=sys.stderr,
	)


if __name__ == "__main__":
	main()
