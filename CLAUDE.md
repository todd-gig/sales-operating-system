---
title: Sales Operating System — Claude Operating Guide
version: 1.0
status: active
created: 2026-04-01
role: project-system-prompt
priority: critical
tags:
  - sales-os
  - recommendation-engine
  - catalog
  - agent-runtime
  - fastapi
  - sqlite
---

# Project Identity

**Sales Operating System (SalesOS)** is a FastAPI + SQLite backend that manages:
- A 214-item product/service master catalog (sourced from `Sales_Operating_System.xlsx`)
- Rules-based upsell, cross-sell, and bundle recommendation engine
- Opportunity lifecycle management
- Agent runtime with approval state machine

The source of truth for catalog data, scoring logic, upsell rules, and bundle definitions is `Sales_Operating_System.xlsx` on the Desktop. The seeder at `scripts/seed_from_xlsx.py` populates the SQLite database from that file.

# Architecture

```
app/
  main.py              — FastAPI app factory, lifespan (DB init + seeder)
  api/routes.py        — All REST endpoints under /api/v1
  models/
    database.py        — SQLite DDL, connection helpers, query utilities
    schemas.py         — Pydantic v2 schemas for every entity
  services/
    recommendation_engine.py  — Upsell / cross-sell / bundle scoring logic
  agents/
    runtime.py         — Agent deployment state machine (draft→ready→running→…)
scripts/
  seed_from_xlsx.py    — Populates DB from Sales_Operating_System.xlsx
```

# Catalog & Scoring Model

**Scoring formula** (from xlsx Scoring_Model sheet):
```
score = ROUND((Interaction_Value * 0.6) + (Marketing_Influence * 0.4), 2)
```
Normalized on a 1–5 scale. `score_multiplier` in the catalog amplifies recommendation confidence.

**Catalog structure** (key fields):
- `ID` — MC-001 through MC-214 (Excel IDs, not DB UUIDs)
- `Type` — Asset | Service | Channel | System | Deliverable
- `Category` — Lead Generation & Conversion | Sales Enablement | Brand & Content | etc.
- `Funnel_Stage` — Awareness | Consideration | Decision | Retention
- `Automation_Potential` — High | Medium | Low

# Recommendation Engine Logic

Located at `app/services/recommendation_engine.py`.

**Upsell**: matched by `primary_product_id` already on the opportunity OR `client_need_state_id` matching `detected_need_summary`. Confidence boosted by dependency satisfaction, need-state alignment, and `score_multiplier`.

**Cross-sell**: matched by `cross_sell_rules` on product pairs. Confidence = `(bundle_strength / 5.0) * score_multiplier`. Bundle strength is 1–5 from the xlsx Cross_Sell_Matrix.

**Bundle**: matched by counting how many bundle products satisfy detected need states. Coverage ratio = overlap / bundle_product_count.

**Final output**: deduplicated by product_id, highest confidence wins, persisted to `recommendations` table.

# Known Bundles (from xlsx)

| Bundle Name | Target Need |
|---|---|
| Acquisition Engine | Demand generation / top-of-funnel |
| Authority Builder | Trust / positioning |
| Sales Accelerator | Close-rate improvement |
| Retention & Expansion System | LTV / retention |
| Omnichannel Activation Pack | Multi-channel reach |

# Agent Runtime State Machine

```
draft → ready → running → awaiting_approval → completed
                                             → failed
                                             → canceled
```

`approval_mode`: `none` | `always` | `on_action`

# Client Need States (from xlsx Client_Needs_Mapping)

| Problem | Priority |
|---|---|
| No clear offer | 1 |
| Low lead volume | 1 |
| Traffic but weak conversion | 1 |
| Weak trust / no proof | 1 |
| No nurture system | 1 |
| No structured sales process | 1 |
| Manual follow-up burden | 1 |
| Inconsistent branding | 2 |
| Low social engagement | 2 |
| No analytics | 2 |

# Development Rules

1. **Never delete catalog items** — mark `is_active = 0` instead.
2. **Score changes go through the xlsx** — update Scoring_Model sheet, re-seed.
3. **Recommendation engine is rules-based** — do not add LLM calls inside scoring paths; LLM belongs in the agent runtime layer.
4. **All DB mutations must use `db.insert()` / `db.update()`** — never raw SQL writes outside `database.py`.
5. **Tests live in `tests/`** — run with `pytest` before any push to main.

# Org Alignment

This project is the commercial intelligence backbone of the broader Gigaton / Carmen Beach / LiquiFex sales architecture. The catalog and upsell logic here feed into:
- **Gigaton**: pricing and margin optimization decisions
- **Carmen Beach**: service packaging and affiliate program design
- **LiquiFex**: portfolio product recommendations

Any changes to scoring weights, catalog items, or bundle definitions must be reviewed against downstream system dependencies.

---

## Doctrine alignment

Single source of truth for first principles, methodology, and anti-patterns:

- [`decision-engine/drift_sentinel/GIGATON_CANONICAL_FIRST_PRINCIPLES.md`](https://github.com/todd-gig/decision-engine/blob/main/drift_sentinel/GIGATON_CANONICAL_FIRST_PRINCIPLES.md) — 7 non-negotiables, 15 first principles, 8 ethos filters, 17 frameworks, 12 anti-patterns
- [`decision-engine/MASTER_FIRST_PRINCIPLES_REFERENCE.md`](https://github.com/todd-gig/decision-engine/blob/main/MASTER_FIRST_PRINCIPLES_REFERENCE.md) — thresholds, weights, formulas, decision pathways

### Doctrine-driven constraints (apply here)

- **CRIT-003** — `app/services/claude_reasoning.py` LLM calls must carry `prompt_version` + `schema_version` (already remediated 2026-05-05 via `_call()` audit envelope)
- **CRIT-007** — every LLM call must accept `provider` + `model` (already remediated 2026-05-05)
- **Development Rule #3 above** ("Recommendation engine is rules-based") aligns with canonical §5.8 (Decision Routing Framework: Python-First when stable + auditable + high-volume) — never weaken
- **Mn-05 open** — seed-file UX: `scripts/seed_from_xlsx.py:468` defaults to `~/Desktop/Sales_Operating_System.xlsx` which doesn't exist in deployment; document fallback behavior or fail-fast at startup. Tracked in BETA_2_GAP_LIST.
- **Mn-06 open** — Cloud Run SQLite is ephemeral; production deployments need GCS FUSE mount (commented in cloudbuild.yaml) or migration to Cloud SQL. Tracked in BETA_2_GAP_LIST.
- **Slack is user-level only** — agent runtime and recommendation engine must not post to Slack autonomously
