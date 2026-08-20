# P11 — Production Agent with Observability

**Brief:** Tracing, latency/cost dashboards, alerting on loops/failures, canary testing, rollback. *Shows: ship to production, not localhost.*

**Status: Partial.** Tracing, dashboards, alerting, offline canary and rollback all work. Live shadow traffic with a percentage split is **not** built — see below.

## Run it

```bash
python3 p11_observability/main.py --dashboard
python3 p11_observability/main.py --alerts
python3 p11_observability/main.py --runs
python3 p11_observability/main.py --trace <run_id>
python3 p11_observability/main.py --seed-canary    # split traffic, then compare
python3 p11_observability/main.py --rollback
python3 -m pytest p11_observability/test_p11.py -v # 12 tests
```

## P11 observes; it does not instrument

The tracer lives in `core/trace.py`, and P1–P8 emit spans as they run. P11 is a **read model** over `data/agent.db`. No project imports P11; no project knows it exists.

That is the whole architectural point: instrumentation cannot be retrofitted. If the other projects were not already writing spans, P11 would have nothing to show — which is exactly what happens at companies that leave observability until last.

`test_dashboard_aggregates_runs_written_by_other_projects` pins this.

## Alert rules

| Rule | Threshold | Why |
|---|---|---|
| `possible_loop` | run reached the iteration cap | The cap stopped it, but hitting the cap is itself a signal |
| `high_failure_rate` | failed runs over window | |
| `span_errors` | repeated failures in one span name | Localises to a component, not just a run |
| `cost_spike` | cost per run above baseline | Catches a routing regression before the invoice does |

Deliberately thresholds, not anomaly detection. A rule an on-call engineer can read at 3am beats a model they have to trust.

## Canary — honest scope

**What works:** prompt versions are a runtime value, not a source constant. `assign_version()` buckets deterministically by `hash(claim_id)` — deterministic, not random, so a retry of the same claim always lands in the same bucket and the comparison is not contaminated. `compare_versions()` scores both over the same set on cost, latency and success. Rollback is a config flip, audited.

Measured on a 50/50 split: v1 $0.000713/run, v2 $0.000815/run — v2's longer prompt costs 14% more, which is the trade the comparison exists to surface.

**What is not built:** live shadow traffic. That needs a request router and duplicate-suppression on side effects, since shadowing a run that issues a payment must not issue two. That is a week of work on its own and I would rather say so than ship a `canary_pct` that does not really split live traffic.

## Known limitations

- **SQLite.** Fine for this; production wants OTel export to Arize, LangSmith or Datadog. The span schema is deliberately OTel-shaped (`span_id`, `parent_id`, `duration_ms`, `attrs`) so that export is a writer, not a migration.
- **No sampling.** Every span is written. At production volume that needs head-based sampling with tail retention for errors.
- **Alerts print; they do not page.** No PagerDuty or webhook sink.
- **Dashboard queries scan the full table.** Indexed on `run_id` only; a time-series rollup is needed beyond ~10⁵ runs.
