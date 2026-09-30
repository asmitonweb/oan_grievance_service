# The catch-all category and type an intake falls back to when the submitter does
# not pick one. Seeded by setup/install.py; draft intake defaults to them by name.
FALLBACK_SERVICE_CATEGORY = "Other"
FALLBACK_GRIEVANCE_TYPE = "Other"


# Grievance Workflow states, seeded by setup/install.py (WORKFLOW_STATES). Code that
# branches on where a case is compares against these, never a bare string.
#
# Draft is the only state at docstatus 0, so "is this still a draft?" is asked of
# docstatus, not of the state name.
STATE_DRAFT = "Draft"
STATE_SUBMITTED = "Submitted"
STATE_ASSIGNED = "Assigned"
STATE_IN_PROGRESS = "In Progress"
STATE_MORE_INFO_NEEDED = "More Info Needed"
STATE_RESOLVED = "Resolved"
STATE_CLOSED = "Closed"
STATE_REJECTED = "Rejected"

# The groups the dashboards count by. Awaiting Action is every state in which an
# officer owes the next move; More Info Needed waits on the submitter, so it is
# open but not awaiting. Resolved on the dashboards means Resolved or Closed.
AWAITING_ACTION_STATES = (STATE_SUBMITTED, STATE_ASSIGNED, STATE_IN_PROGRESS)
OPEN_STATES = (*AWAITING_ACTION_STATES, STATE_MORE_INFO_NEEDED)
RESOLVED_STATES = (STATE_RESOLVED, STATE_CLOSED)


# Notification event codes. Each is the "method" on one core Notification record per
# channel, seeded by setup/install.py and editable from the desk thereafter.
EVENT_SUBMISSION_RECEIVED = "submission_received"
EVENT_DUPLICATE_DETECTED = "duplicate_detected"
EVENT_ASSIGNED_AUTO = "grievance_assigned_auto"
EVENT_ASSIGNED_MANUAL = "grievance_assigned_manual"
EVENT_STATUS_IN_PROGRESS = "status_in_progress"
EVENT_MORE_INFO_REQUESTED = "more_info_requested"
EVENT_SUBMITTER_RESPONDED = "submitter_responds_to_info"
EVENT_RESPONSE_SENT = "structured_response_sent"
EVENT_CONFIRMATION_WINDOW = "confirmation_window_open"
EVENT_CONFIRMED = "grievance_confirmed"
EVENT_REOPENED = "grievance_reopened"
EVENT_AUTO_CLOSED = "auto_closed_no_response"
EVENT_CLOSED = "grievance_closed"
EVENT_STATUS_REJECTED = "status_rejected"
EVENT_SLA_REMINDER_50 = "sla_reminder_50"
EVENT_SLA_REMINDER_80 = "sla_reminder_80"
EVENT_SLA_AT_RISK = "sla_at_risk_report"
EVENT_SLA_BREACH = "sla_breached"
EVENT_MANUAL_ESCALATION = "manual_escalation"
EVENT_REASSIGNMENT_REQUESTED = "reassignment_requested"
EVENT_ANONYMITY_DISCLOSURE_REQUESTED = "anonymity_disclosure_requested"

# Default submitter confirmation window.
DEFAULT_CONFIRMATION_DAYS = 7
# Reminder thresholds as a percentage of the SLA window.
SLA_REMINDER_THRESHOLDS = (50, 80)
# Global default ceiling on a single deferral.
DEFAULT_MAX_DEFERRAL_DAYS = 30


# Canonical Frappe user roles used for API authentication and authorization.
ROLE_SUBMITTER = "Grievance Submitter"
ROLE_OFFICER = "Grievance Officer"
ROLE_ADMIN = "Grievance Admin"

STAFF_ROLES = frozenset({ROLE_OFFICER, ROLE_ADMIN, "System Manager", "Administrator"})
ALLOWED_GRIEVANCE_ROLES = [
	ROLE_SUBMITTER,
	ROLE_OFFICER,
	ROLE_ADMIN,
	"System Manager",
	"Administrator",
]
