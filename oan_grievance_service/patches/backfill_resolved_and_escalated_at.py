# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

"""Fill `resolved_at` and `escalated_at` for cases that predate the fields.

`resolved_at` is the last time the case entered Resolved or Closed from an open
state, read from the Status History; `escalated_at` is the first escalation entry
on its Timeline. A case with no such row falls back to its last modification,
which is the best record left of when it got there.
"""

import frappe


def execute():
	frappe.reload_doc("grievance_management", "doctype", "grievance")

	frappe.db.sql(
		"""UPDATE `tabGrievance` g
		LEFT JOIN (
			SELECT grievance, MAX(`timestamp`) AS resolved_at
			FROM `tabGrievance Status History`
			WHERE to_status IN ('Resolved', 'Closed')
				AND COALESCE(from_status, '') NOT IN ('Resolved', 'Closed')
			GROUP BY grievance
		) h ON h.grievance = g.name
		SET g.resolved_at = COALESCE(h.resolved_at, g.modified)
		WHERE g.status IN ('Resolved', 'Closed') AND g.resolved_at IS NULL"""
	)

	frappe.db.sql(
		"""UPDATE `tabGrievance` g
		LEFT JOIN (
			SELECT grievance, MIN(COALESCE(created_on, creation)) AS escalated_at
			FROM `tabGrievance Timeline`
			WHERE entry_type = 'escalation'
			GROUP BY grievance
		) t ON t.grievance = g.name
		SET g.escalated_at = COALESCE(t.escalated_at, CASE WHEN g.escalated = 1 THEN g.modified END)
		WHERE g.escalated_at IS NULL AND (t.grievance IS NOT NULL OR g.escalated = 1)"""
	)
