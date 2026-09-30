# Onboarding OAN Grievance Service to Kong Gateway

This guide covers deploying the OAN Grievance Service (`oan_grievance_service`) REST API behind Kong Gateway, wiring authentication, setting tiered rate limits, and managing declarative deployments via [decK](https://github.com/Kong/deck).

`kong.yml` is generated directly from `../openapi/openapi_v1.public.yaml` using `generate_kong_config_from_spec.py`.

---

## 1. Architecture

```
Citizen App / Web / Staff  →  Kong Gateway  →  OAN Grievance Service (Frappe)
                               TLS · authn ·      REST API (@route)
                               throttling ·       Grievance intake, tracking,
                               observability      lifecycle & resolution
```

Kong fronts the grievance service:

- **TLS Termination & Global Hygiene:** CORS headers, request correlation IDs (`X-Request-Id`), and payload size limiting (15 MB safety limit).
- **Rate-Limiting Tiers:** Segregated rate limits protecting public reference data from abuse while giving high-throughput capacity to staff operations.
- **JWT Authentication:** Protects authenticated endpoints (intake, tracking, replies, notes, lifecycle actions) using Bearer tokens minted by `oan_auth_service`.
- **Dashboard key:** The public dashboard charts (`/api/v1/charts/<chart_id>`, `DashboardKeyAuth` in the spec) are read by the OAN dashboards, a server rather than a user. They carry `key-auth` (header `apikey`, hidden from the upstream) and an `acl` admitting only the `oan-dashboards` consumer's group `dashboards`.

---

## 2. Declarative Deployment with decK (DB-less)

The API routing surface is managed as version-controlled declarative state:

```bash
# 1. Regenerate OpenAPI spec (if endpoints or schemas changed)
python3 ../openapi/generate_openapi_spec.py

# 2. Regenerate Kong declarative config
python3 generate_kong_config_from_spec.py

# 3. Validate configuration syntax
deck validate -s kong.yml

# 4. Diff against live gateway
deck diff -s kong.yml --kong-addr https://kong-admin.internal:8001

# 5. Sync to Kong Gateway
deck sync -s kong.yml --kong-addr https://kong-admin.internal:8001
```

---

## 3. Throttling Tiers

| Tier                | Keyed By  | Limit                  | Purpose                                                                                                                              |
| :------------------ | :-------- | :--------------------- | :----------------------------------------------------------------------------------------------------------------------------------- |
| `public-reference`  | Client IP | 120 / min, 3,000 / hr  | Public unauthenticated queries: health probes, ping, submitter options, administrative area hierarchies.                             |
| `citizen-intake`    | Consumer  | 60 / min, 1,000 / hr   | Citizen actions: lodging grievances, tracking by ticket number, replies, conversation messages, reopen, and resolution confirmation. |
| `officer-core`      | Consumer  | 300 / min, 10,000 / hr | Back-office / staff workflows: multi-select grievance search and listing, officer dropdown options, internal case notes.             |
| `public-dashboards` | Consumer  | 120 / min, 3,000 / hr  | Dashboard charts read by the OAN dashboards with their key (one consumer, `oan-dashboards`).                                         |

Counters use `policy: redis` to synchronize rate-limiting across distributed Kong nodes.

---

## 4. Configuration Placeholders

Before syncing `kong.yml` to production:

1. **Upstream URL:** Replace `GRIEVANCE_UPSTREAM_URL` (defaults to `http://oan-grievance.internal.svc:8000`) with your production service address.
2. **JWT Secret:** In `consumers[0].jwt_secrets`, set `secret` to match `jwt_secret` from your `site_config.json` or secrets manager.
3. **Redis Host/Port:** Configure Kong's rate-limiting plugin to connect to your central Redis cluster or Sentinel.
4. **Dashboard key:** The `oan-dashboards` consumer's key is `${{ env "DECK_OAN_DASHBOARDS_API_KEY" }}`. Export it for `deck sync` from the secrets manager, and give the same key to the OAN dashboards (`GRIEVANCE_DASHBOARD_API_KEY` in their Secret). It never appears in this repo.
