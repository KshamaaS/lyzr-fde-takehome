# Handover runbook

For the engineer who owns this after me.

## Deploy

**Control plane (HF Spaces):** push `app/`, `core/`, `p*/`, `data/`, `pipeline.py`.
Entry point `app/streamlit_app.py`. Free CPU tier is sufficient.

**Environment**

| Var | Default | Notes |
|---|---|---|
| `PROVIDER` | `mock` | `mock` \| `anthropic` \| `lyzr` |
| `ANTHROPIC_API_KEY` | — | required when `PROVIDER=anthropic` |
| `LYZR_API_KEY` / `LYZR_AGENT_ID` / `LYZR_KB_ID` | — | required when `PROVIDER=lyzr` |
| `RETRIEVER` | `local` | `lyzr` to use the managed Knowledge Base (P2) |
| `AGENT_DB` | `data/agent.db` | move to a volume in production |
| `P6_MAX_USD` | `5000` | auto-approve ceiling |
| `P6_MIN_CONF` | `0.70` | confidence floor |
| `P6_TTL_S` | `172800` | approval SLA (48h) |

## What pages at 3am

| Alert | Meaning | First action |
|---|---|---|
| `approval_sla_breach` | Claims past their deadline unactioned | Page the claims supervisor, not engineering. Run the SLA sweep. |
| `failure_rate` >20% | Provider outage or schema drift | Check `by_model` success % in the dashboard; flip `PROVIDER`. |
| `possible_loop` | Planner not converging | Inspect the span tree; the iteration cap already contained it. |
| `cost_spike` | Runaway spend on one run | Check budget enforcement in P7; lower `DEFAULT_BUDGET_USD`. |

## Known operational gaps

1. **SQLite, single writer.** Fine for the demo, wrong for production. Move to
   Postgres; the schema ports unchanged.
2. **No worker crash recovery (P8).** An event stuck in `processing` because the
   worker died is never reclaimed. Needs a lease with a heartbeat, or a reaper
   for stale rows. **This is the first thing I would build.**
3. **No auth on the control plane.** Approver identity is a free-text field.
   Real deployment needs SSO and a signed approver identity — the audit trail is
   only as good as the identity in it.
4. **Confidence is uncalibrated.** Applies to P1, P6 and P7 alike. Calibrating
   needs ~100 labelled historical adjudications; flagged as a week-one
   dependency in the scoping note.
5. **P11 canary is offline A/B**, not live shadow traffic. Live shadowing needs
   a request router and duplicate-suppression on side effects.

## Changing the things a customer will ask to change

- **Approval threshold** → `P6_MAX_USD` env var. No deploy.
- **Add a tool** → one `@registry.register(...)` decorator in
  `p04_tool_orchestrator/tools.py` with capability tags, a required role, and a
  Pydantic return schema. ~15 lines plus a test.
- **Change conflict precedence** → the `PRECEDENCE` list in
  `p04_tool_orchestrator/main.py`. Ordered most-restrictive-first; adding a rule
  is one tuple.
- **Swap fraud vendor** → register the new tool with the same `fraud` capability
  tag and a `reliability` estimate. The planner does not change.
- **Add a policy document** → drop a Markdown file with `## SEC-N Title`
  headers into `data/policy_docs/`.
