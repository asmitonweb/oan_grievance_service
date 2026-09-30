# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class GrievanceStatDaily(Document):
	"""Written only by services.dashboard_rollup; see that module."""


def on_doctype_update():
	frappe.db.add_index("Grievance Stat Daily", ["stat_date", "region", "service_category"])
