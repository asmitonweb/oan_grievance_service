# Dashboard analytics

The grievance dashboards (the officer/admin dashboard and the public OAN programme
dashboards) are served from two rollup tables that the scheduler refreshes every 15
minutes. No dashboard request ever aggregates `tabGrievance`.

## Data flow

```
every 15 min (cron */15)      services/dashboard_rollup.refresh()
  tabGrievance, Status History, Duplicate, Feedback
      -> Grievance Stat Daily      events per day (last 3 days rebuilt; all nightly)
      -> Grievance Stat Snapshot   status counts per day (today replaced; history kept)
      -> as_of (system default)

request                        services/dashboard.get_chart()
  Redis cache per chart + parameters, keyed on as_of
      -> rows from the rollups only
```

- **Grievance Stat Daily** holds counts per `stat_date, region, service_category, assigned_dept`:
  filed, resolved (split on time / after SLA), resolution hours, rejected, first escalated,
  feedback and satisfied feedback (rating 4 or 5). Events never move once they happen, so a
  refresh rebuilds only the last three days; the nightly run rebuilds every day.
- **Grievance Stat Snapshot** holds how many cases stood in each status on a day, by the same
  dimensions plus `escalated`, `sla_state` (ok / at_risk / breached / paused / no_sla),
  cases awaiting duplicate review, and the oldest open, non-escalated filing date. Today's rows
  are replaced on every refresh; earlier days are kept, because a past day's status counts
  cannot be rebuilt from current data.
- Only counts, sums and minimums are stored. Rates and averages are worked out when a chart is
  read, so every filter combination is exact.
- A region is the Region-level ancestor of the filing area, stored as its P-code (`ET04`).
- Refreshes never overlap: each takes a database lock and a second run skips.

Staleness: at most 15 minutes here, plus whatever the consumer caches (the OAN dashboards
cache another 15 minutes).

## Definitions

| Term            | Meaning                                                                      |
| --------------- | ---------------------------------------------------------------------------- |
| Total           | Every grievance except Draft                                                 |
| Awaiting Action | Submitted + Assigned + In Progress (an officer owes the next move)           |
| Open            | Awaiting Action + More Info Needed                                           |
| Resolved        | Resolved + Closed; `resolved_at` is when the case last reached either        |
| Escalated       | Open cases with the `escalated` flag; `escalated_at` is the first escalation |
| At risk         | Open, clock running, due within 24 hours                                     |
| Breached        | Open, clock running, past `sla_due_date`                                     |
| On time         | Resolved at or before `sla_due_date`; cases without an SLA count as neither  |

## Endpoints

| Route                           | Access          | Returns                                                    |
| ------------------------------- | --------------- | ---------------------------------------------------------- |
| `GET /api/v1/charts/<chart_id>` | public, no auth | one chart; public charts only; counts, never case detail   |
| `GET /api/v1/charts?charts=...` | Grievance Admin | several charts per call, admin-only charts and live detail |

Both return the standard envelope with `meta.as_of`. Filters: `region` (P-codes),
`service_category` (alias `category`), `from` / `to`, `month`, `granularity`; the admin form
also takes `assigned_dept` and `limit`. An unknown value is a 400 naming the field.

Each public chart is its own literal route, because the JWT middleware exempts guest routes
by exact path; an admin-only or unknown chart id is therefore never reachable without a token.

**Through the gateway.** The public chart routes are guest to the platform, but the spec marks them
`DashboardKeyAuth`: once Kong enforces authorization, it requires the OAN dashboards' API key
(consumer `oan-dashboards`, group `dashboards`) and strips it before forwarding. The dashboards
already send the key when configured; until the gateway enforces keys the header is ignored.

Public charts: `grvKpis`, `grvPerformanceKpis`, `grvMonthlyTrend`, `grvWeeklyTrend`,
`grvNetBacklogTrend`, `grvStatusDistribution`, `grvByCategory`, `grvCategoryResolution`,
`grvResolutionRateByRegion`, `grvSlaRisk`, `grvPendingDuplicates`, `grvOldestOpen` (age only),
`grvFilterRegions`, `grvFilterCategories`. Admin only: `grvRecent`, `grvFilterDepartments`, and
`grvOldestOpen` with the case named. Row shapes are in `openapi/openapi_v1.yaml` and
`postman/oan_grievance_dashboard_charts_collection.json`.

## Operations

- First build on a site: `after_migrate` queues a full refresh when the rollups are empty. By hand:
  `bench --site <site> execute oan_grievance_service.services.dashboard_rollup.refresh --kwargs "{'full': True}"`.
- A new chart is a builder in `services/dashboard.py` plus an entry in `CHARTS`; a new metric is
  a column on a rollup doctype plus its SQL in `services/dashboard_rollup.py`.
