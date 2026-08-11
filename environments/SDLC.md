# Openflow SDLC with NiFi Hub

## Architecture Diagram

```
                         SOFTWARE DEVELOPMENT LIFECYCLE
                        Openflow + NiFi Hub (GitOps CD)

================================================================================

  DEVELOPER WORKFLOW (Git Branching Strategy)
  ============================================

  ┌─────────┐    PR/Merge     ┌─────────┐    PR/Merge     ┌─────────┐
  │   dev   │ ──────────────> │   qua   │ ──────────────> │   prd   │
  │ branch  │                 │ branch  │                 │ branch  │
  └────┬────┘                 └────┬────┘                 └────┬────┘
       │                           │                           │
       │ push triggers             │ push triggers             │ push triggers
       │ deploy-dev.yml            │ deploy-qua.yml            │ deploy-prd.yml
       │                           │                           │
       ▼                           ▼                           ▼

  GITHUB ACTIONS (CI/CD Pipeline)
  ============================================

  ┌──────────────────────────────────────────────────────────────────────┐
  │  GitHub Repository: sfc-gh-ohutuleac/nifihub                         │
  │                                                                      │
  │  /flows/examples/                                                    │
  │    └── example-flow-to-snowflake.json   ◄── Flow definitions (JSON)  │
  │                                                                      │
  │  /environments/                                                      │
  │    ├── dev/config.yaml   ◄── Points to branch: dev                   │
  │    ├── qua/config.yaml   ◄── Points to branch: qua                   │
  │    └── prd/config.yaml   ◄── Points to branch: prd                   │
  │                                                                      │
  │  /.github/workflows/                                                 │
  │    ├── deploy-dev.yml    ◄── Triggered on push to dev                │
  │    ├── deploy-qua.yml    ◄── Triggered on push to qua                │
  │    └── deploy-prd.yml    ◄── Triggered on push to prd                │
  └──────────────────────────────────────────────────────────────────────┘
       │                           │                           │
       │ run-cd.py                 │ run-cd.py                 │ run-cd.py
       │ (describe→diff→           │                           │
       │  translate→apply)         │                           │
       ▼                           ▼                           ▼

  SNOWFLAKE ACCOUNT (Openflow Platform)
  ============================================

  ┌──────────────────────────────────────────────────────────────────────────┐
  │                                                                          │
  │  DEPLOYMENT: NIFIHUB_DEPLOYMENT                                          │
  │  ═══════════════════════════════                                         │
  │  A logical grouping that owns one or more runtimes.                      │
  │  Maps to a Snowflake Openflow Deployment object.                         │
  │                                                                          │
  │  ┌────────────────────┐  ┌────────────────────┐  ┌─────────────────────┐ │
  │  │  RUNTIME: DEV      │  │  RUNTIME: QUA      │  │  RUNTIME: PRD       │ │
  │  │  (SMALL, 1 node)   │  │  (SMALL, 1 node)   │  │  (MEDIUM, 2n)       │ │
  │  │                    │  │                    │  │                     │ │
  │  │  Registry: dev     │  │  Registry: qua     │  │  Registry: prd      │ │
  │  │  branch            │  │  branch            │  │  branch             │ │
  │  │                    │  │                    │  │                     │ │
  │  │ ┌────────────────┐ │  │ ┌────────────────┐ │  │ ┌────────────────┐  │ │
  │  │ │ Process Group: │ │  │ │ Process Group: │ │  │ │ Process Group: │  │ │
  │  │ │ Example Flow   │ │  │ │ Example Flow   │ │  │ │ Example Flow   │  │ │
  │  │ │ to Snowflake   │ │  │ │ to Snowflake   │ │  │ │ to Snowflake   │  │ │
  │  │ │ (v: 2)         │ │  │ │ (v: 1)         │ │  │ │ (v: latest)    │  │ │
  │  │ └────────────────┘ │  │ └────────────────┘ │  │ └────────────────┘  │ │
  │  └────────────────────┘  └────────────────────┘  └─────────────────────┘ │
  │             ▼                       ▼                     ▼              │
  │  ┌──────────────────────────────────────────────────────────────┐        │
  │  │  OPENFLOW.OPENFLOW.EXAMPLE_FLOW (Snowflake Table)            │        │
  │  │  ┌──────────┬────────────────────┬─────────┬───────────────┐ │        │
  │  │  │ MESSAGE  │ SOURCE             │ RUNTIME │ TIMESTAMP     │ │        │
  │  │  ├──────────┼────────────────────┼─────────┼───────────────┤ │        │
  │  │  │ Hello... │ GenerateFlowFile   │ dev-01  │ 2026-08-11... │ │        │
  │  │  └──────────┴────────────────────┴─────────┴───────────────┘ │        │
  │  └──────────────────────────────────────────────────────────────┘        │
  └──────────────────────────────────────────────────────────────────────────┘
```

## Key Concepts

### Openflow Deployment

A top-level Snowflake object that represents a managed NiFi environment. Contains one or more Runtimes. Created via SQL:

```sql
CREATE OPENFLOW DEPLOYMENT <name>
```

### Openflow Runtime

A running Apache NiFi instance managed by Snowflake. Each runtime has its own NiFi canvas, processors, and REST API endpoint. Configured with:

- `node_type` (SMALL/MEDIUM/LARGE)
- `min_nodes` / `max_nodes`
- Network rules for egress
- `execute_as_role` for Snowflake access

Created via SQL:

```sql
CREATE OPENFLOW RUNTIME <deployment>.<name>
```

### Connector

A pre-built, Snowflake-managed NiFi flow template for common integrations (Salesforce, SharePoint, Slack, Postgres CDC). Deployed via SQL:

```sql
CREATE OPENFLOW CONNECTOR <name>
```

Connectors are versioned and upgraded independently of custom flows.

### GitHub Flow Registry Client

A NiFi Registry Client that reads versioned flow definitions directly from a GitHub repository. Each commit to a flow JSON file on the configured branch creates a new "version" in NiFi.

Configuration:

| Property | Value |
|----------|-------|
| Repository Owner | `sfc-gh-ohutuleac` |
| Repository Name | `nifihub` |
| Default Branch | `dev` / `qua` / `prd` (branch = environment) |
| Repository Path | `flows` |

The registry client maps the repo structure to NiFi concepts:

```
flows/<bucket>/<flow>.json  →  bucket/flow/version
e.g. flows/examples/example-flow-to-snowflake.json
     → bucket: examples, flow: example-flow-to-snowflake
```

## Promotion Flow

```
  Developer              DEV                  QUA                  PRD
  ─────────────────────────────────────────────────────────────────────────────────────────

  1. Edit flow ───────────┐
     JSON locally         │
                          ▼
  2. Push to             [dev branch]
     dev branch          GitHub Action
                         runs CD ──────> NIFIHUB_RUNTIME_DEV
                         (auto-deploy)   gets new flow version
                          │
  3. Test in DEV          │ verify flow works
                          │
  4. PR: dev→qua          └───────────────────┐
                                              ▼
                                              [qua branch]
                                              GitHub Action
                                              runs CD ──────> NIFIHUB_RUNTIME_QUA
                                              (auto-deploy)   gets new flow version
                                              │
  5. QA test                                  │ validate in QUA
                                              │
  6. PR: qua→prd                              └───────────────────┐
                                                                  ▼
                                                                  [prd branch]
                                                                  GitHub Action
                                                                  runs CD ──────> NIFIHUB_RUNTIME_PRD
                                                                  (auto-deploy)   production deployment
```

## Concept Summary

| Concept | What It Is | Analogy |
|---------|-----------|---------|
| **Deployment** | Logical container for runtimes in Snowflake | A "project" or "namespace" |
| **Runtime** | A running NiFi instance (compute + canvas) | A "server" or "cluster" |
| **Connector** | Pre-built Snowflake-managed flow for SaaS sources | A "managed integration" |
| **GitHub Flow Registry** | NiFi reads flow versions directly from Git branches | Git = your artifact registry |
| **NiFi Hub CD** | GitOps pipeline: describe live → diff → apply | Like Terraform for NiFi |

## CD Pipeline (run-cd.py)

The pipeline performs 4 steps:

1. **Describe** — queries live Snowflake + NiFi REST API state
2. **Diff** — compares live state against desired config.yaml
3. **Translate** — converts diffs into actionable change operations
4. **Apply** — executes changes (create/modify/delete deployments, runtimes, flows)

The branch-per-environment pattern means each runtime's registry client points to a different branch. Promoting a flow from DEV to QUA is simply merging `dev` into `qua` — the CD pipeline auto-detects the new version and deploys it.
