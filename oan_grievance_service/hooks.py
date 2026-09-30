app_name = "oan_grievance_service"
app_title = "Grievance Management"
app_publisher = "COSS - Centre for Open Societal Systems"
app_description = "OpenAgriNet Ethiopia Grievance Management Module: multi-channel grievance intake, routing, SLA tracking and escalation"
app_email = "admin@openagrinet.org"
app_license = "mit"

# Apps
# ------------------

required_apps = ["oan_auth_service"]

add_to_apps_screen = [
	{
		"name": "oan_grievance_service",
		"title": "Grievance Management",
		"route": "/app/grievance",
	}
]

# Register Werkzeug REST routes for Frappe API Map
before_request = ["oan_grievance_service.api.router.ensure_routes_registered"]

# Installation
# ------------------

# Roles, escalation levels, categories, regions and the notification matrix
# are seeded so a fresh site comes up usable.

after_install = "oan_grievance_service.setup.install.after_install"
after_migrate = [
	"oan_grievance_service.setup.install.after_migrate",
	"oan_grievance_service.services.dashboard_rollup.ensure_built",
]

# Permissions
# ------------------
# Deny-by-default RBAC. The query condition filters list views, reports and
# the API uniformly; has_permission mirrors it for a single document.

permission_query_conditions = {
	"Grievance": "oan_grievance_service.permissions.grievance_query_conditions",
}

has_permission = {
	"Grievance": "oan_grievance_service.permissions.has_grievance_permission",
	# Core's File resolves a private file's permission against whatever it is
	# attached to, so this is what stops /private/files/<name> serving an unscanned
	# object behind download()'s back.
	"Grievance Attachment": (
		"oan_grievance_service.grievance_management.doctype.grievance_attachment"
		".grievance_attachment.has_permission"
	),
}

# Document Events
# ------------------
# The lifecycle is the Grievance Workflow record (setup/install.py); the
# Grievance controller records each move from the save Frappe's engine makes.
# A structured response advances the lifecycle. Every read of a case is audited.

_CLEAR_LOOKUP_CACHE = {
	"on_update": "oan_grievance_service.api.v1._options.clear_reference_cache",
	"after_delete": "oan_grievance_service.api.v1._options.clear_reference_cache",
}
doc_events = {
	"Grievance": {
		"onload": "oan_grievance_service.services.audit.on_grievance_view",
	},
	"Grievance Response": {
		"after_insert": "oan_grievance_service.services.hooks_handlers.response_after_insert",
	},
	# Our send path renders per recipient inside print_language(), which only
	# moves _()-marked strings, so a Grievance notification must not carry bare literal
	# text. Extends a core doctype through the supported hook rather than editing it.
	"Notification": {
		"validate": "oan_grievance_service.services.notifications.validate_notification",
	},
}

# Scheduled Tasks
# ------------------
# A background process monitors open grievances against their SLA deadlines.
# The batch must complete within 30 minutes.

scheduler_events = {
	"hourly": [
		"oan_grievance_service.tasks.send_sla_reminders",
		"oan_grievance_service.tasks.escalate_breached",
		"oan_grievance_service.tasks.expire_state_timers",
		"oan_grievance_service.tasks.forward_stale_change_requests",
		"oan_grievance_service.tasks.dispatch_notifications",
		# Attachments land as Pending and is_servable() withholds anything not yet
		# Clean, so without this every uploaded file stays invisible to officers.
		"oan_grievance_service.tasks.scan_pending_attachments",
		# Process unrouted submitted cases through the routing engine in the background
		"oan_grievance_service.tasks.drain_routing_queue",
	],
	"daily": [
		"oan_grievance_service.tasks.purge_expired_drafts",
	],
	# The dashboards read only these rollups, so this is how fresh they are.
	"cron": {
		"*/15 * * * *": [
			"oan_grievance_service.tasks.refresh_dashboard_rollup",
		],
	},
	"daily_long": [
		"oan_grievance_service.tasks.rebuild_dashboard_rollup",
	],
}


# Authentication & Profile Resolution
# -----------------------------------
# Integrates with oan_auth_service to enrich user introspection (GET /api/v1/auth/me)
# with grievance profile data. Submitter profile registration is handled via REST API.

on_user_profile = ["oan_grievance_service.api.v1.profile.resolve_user_profile_hook"]
on_user_registered = ["oan_grievance_service.api.v1.profile.on_user_registered_hook"]

# Portal
# ------------------
# The web portal is a channel open to all submitter types.

website_route_rules = [
	{"from_route": "/grievance/track/<path:ticket>", "to_route": "grievance-track"},
]
