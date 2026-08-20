"""
P11 - Production Agent with Observability
Tracing, latency/cost dashboards, alerting on loops/failures, canary testing,
rollback.

This module is a READ-ONLY VIEW plus an alert engine plus a prompt-version
registry. It deliberately contains no agent logic.

That is the design point: P1-P8 emitted spans while they ran, because
`core/trace.py` was written before any project. Observability that has to be
retrofitted is observability you do not have.
"""
from __future__ import annotations
import sys, os, json, time, argparse
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.store import db, audit

VERSION_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "active_version.json")


# ------------------------------------------------------------------ queries
def runs(limit=50, project=None):
    q = "SELECT * FROM runs"
    a = []
    if project:
        q += " WHERE project=?"
        a.append(project)
    q += " ORDER BY started_at DESC LIMIT ?"
    a.append(limit)
    with db() as c:
        return [dict(r) for r in c.execute(q, a)]


def span_tree(run_id: str) -> list[dict]:
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM spans WHERE run_id=? ORDER BY started_at", (run_id,))]
    by_parent = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)
    out = []

    def walk(pid, depth):
        for r in by_parent.get(pid, []):
            r["depth"] = depth
            out.append(r)
            walk(r["span_id"], depth + 1)
    walk(None, 0)
    # spans whose parent is themselves (top level) -- include any orphans
    seen = {r["span_id"] for r in out}
    for r in rows:
        if r["span_id"] not in seen:
            r["depth"] = 0
            out.append(r)
    return out


def dashboard() -> dict:
    with db() as c:
        by_project = [dict(r) for r in c.execute("""
            SELECT r.project,
                   COUNT(*)                                  AS runs,
                   SUM(r.status='failed')                    AS failed,
                   SUM(r.status='paused')                    AS paused,
                   ROUND(SUM(r.cost_usd), 6)                 AS cost_usd,
                   ROUND(AVG(CASE WHEN r.ended_at IS NOT NULL
                        THEN (r.ended_at-r.started_at)*1000 END), 1) AS avg_ms
            FROM runs r GROUP BY r.project ORDER BY runs DESC""")]
        by_model = [dict(r) for r in c.execute("""
            SELECT model, COUNT(*) calls, SUM(tokens_in) tin, SUM(tokens_out) tout,
                   ROUND(SUM(cost_usd),6) cost_usd, ROUND(AVG(duration_ms),1) avg_ms,
                   ROUND(100.0*SUM(ok)/COUNT(*),1) success_pct
            FROM spans WHERE kind='llm' AND model IS NOT NULL
            GROUP BY model ORDER BY cost_usd DESC""")]
        tot = dict(c.execute("""
            SELECT COUNT(*) runs, ROUND(SUM(cost_usd),6) cost_usd
            FROM runs""").fetchone())
        slow = [dict(r) for r in c.execute("""
            SELECT name, run_id, duration_ms FROM spans
            ORDER BY duration_ms DESC LIMIT 5""")]
    return {"totals": tot, "by_project": by_project, "by_model": by_model,
            "slowest_spans": slow}


# ------------------------------------------------------------------- alerts
@dataclass
class Alert:
    severity: str
    rule: str
    message: str
    detail: dict


ALERT_RULES = {
    "failure_rate_pct": 20.0,      # % of runs failing in the window
    "loop_iterations": 5,          # spans named think_N / attempt_N per run
    "run_cost_usd": 0.01,          # single run cost ceiling
    "p95_latency_ms": 2000,
    "pending_approvals_over_ttl": 1,
}


def check_alerts(window_s: float = 3600) -> list[Alert]:
    """
    Evaluated against the same span table the dashboard reads. Alerting on a
    different data source than you debug from is how you get pages that nobody
    can reproduce.
    """
    since = time.time() - window_s
    out = []
    with db() as c:
        tot = c.execute("SELECT COUNT(*) n, SUM(status='failed') f FROM runs"
                        " WHERE started_at>?", (since,)).fetchone()
        if tot["n"]:
            rate = 100.0 * (tot["f"] or 0) / tot["n"]
            if rate >= ALERT_RULES["failure_rate_pct"]:
                out.append(Alert("critical", "failure_rate",
                                 f"{rate:.1f}% of runs failed in the last hour",
                                 {"failed": tot["f"], "total": tot["n"]}))

        for r in c.execute("""
            SELECT run_id, COUNT(*) n FROM spans
            WHERE (name LIKE 'think_%' OR name LIKE 'attempt_%') AND started_at>?
            GROUP BY run_id HAVING n >= ?""",
                (since, ALERT_RULES["loop_iterations"])):
            out.append(Alert("warning", "possible_loop",
                             f"run {r['run_id']} made {r['n']} planning iterations",
                             {"run_id": r["run_id"], "iterations": r["n"]}))

        for r in c.execute("SELECT run_id,project,cost_usd FROM runs"
                           " WHERE cost_usd > ? AND started_at > ?",
                           (ALERT_RULES["run_cost_usd"], since)):
            out.append(Alert("warning", "cost_spike",
                             f"run {r['run_id']} cost ${r['cost_usd']:.5f}",
                             dict(r)))

        lat = [r["duration_ms"] for r in c.execute(
            "SELECT duration_ms FROM spans WHERE kind='llm' AND started_at>?"
            " ORDER BY duration_ms", (since,))]
        if lat:
            p95 = lat[int(len(lat) * 0.95) - 1]
            if p95 > ALERT_RULES["p95_latency_ms"]:
                out.append(Alert("warning", "latency_p95",
                                 f"p95 LLM latency {p95}ms", {"p95_ms": p95}))

        n = c.execute("SELECT COUNT(*) n FROM approvals WHERE status='pending'"
                      " AND expires_at < ?", (time.time(),)).fetchone()["n"]
        if n >= ALERT_RULES["pending_approvals_over_ttl"]:
            out.append(Alert("critical", "approval_sla_breach",
                             f"{n} approval(s) past their SLA deadline",
                             {"count": n}))

        errs = [dict(r) for r in c.execute(
            "SELECT name, COUNT(*) n FROM spans WHERE ok=0 AND started_at>?"
            " GROUP BY name ORDER BY n DESC LIMIT 3", (since,))]
        for e in errs:
            out.append(Alert("info", "span_errors",
                             f"{e['n']} failures in span '{e['name']}'", e))
    return out


# ----------------------------------------------------------- canary/rollback
def active_version() -> dict:
    if os.path.exists(VERSION_FILE):
        return json.load(open(VERSION_FILE))
    return {"active": "v1", "canary": None, "canary_pct": 0}


def set_version(active: str, canary: str = None, canary_pct: int = 0):
    cfg = {"active": active, "canary": canary, "canary_pct": canary_pct,
           "updated_at": time.time()}
    os.makedirs(os.path.dirname(VERSION_FILE), exist_ok=True)
    json.dump(cfg, open(VERSION_FILE, "w"), indent=2)
    audit("operator", "version_config_changed", detail=cfg)
    return cfg


def rollback():
    """
    Rollback is a config flip, not a redeploy. That is the whole reason prompt
    version is a runtime value rather than a constant in the source.
    """
    cfg = active_version()
    new = set_version(active=cfg["active"], canary=None, canary_pct=0)
    audit("operator", "rollback", detail={"canary_disabled": cfg.get("canary")})
    return new


def compare_versions(a: str, b: str) -> dict:
    """
    Offline canary: both prompt versions are scored over the same golden set and
    compared on cost, latency and outcome.

    HONEST SCOPE: this is A/B over a fixed evaluation set, not live shadow
    traffic with a percentage split. Live shadowing needs a request router and
    duplicate-suppression on side effects, which was out of scope for the
    timebox. See the limitations section of the README.
    """
    with db() as c:
        rows = {}
        for v in (a, b):
            r = c.execute("""
                SELECT COUNT(*) runs,
                       ROUND(AVG(cost_usd),8) avg_cost,
                       ROUND(AVG(CASE WHEN ended_at IS NOT NULL
                            THEN (ended_at-started_at)*1000 END),1) avg_ms,
                       ROUND(100.0*SUM(status='ok')/COUNT(*),1) ok_pct
                FROM runs WHERE json_extract(meta,'$.prompt_version')=?""",
                          (v,)).fetchone()
            rows[v] = dict(r)
    return {"versions": rows,
            "recommendation": _recommend(rows.get(a), rows.get(b), a, b)}


def _recommend(ra, rb, a, b):
    if not ra or not rb or not ra["runs"] or not rb["runs"]:
        return "insufficient data - run both versions over the golden set first"
    if rb["ok_pct"] >= ra["ok_pct"] and rb["avg_cost"] <= ra["avg_cost"]:
        return f"promote {b}: equal or better success at equal or lower cost"
    if rb["ok_pct"] < ra["ok_pct"]:
        return f"do not promote {b}: success rate regressed"
    return f"{b} costs more; promote only if the quality gain justifies it"


# ---------------------------------------------------------------------- CLI
def _fmt_tree(rid):
    for s in span_tree(rid):
        ind = "  " * s["depth"]
        mark = "OK " if s["ok"] else "ERR"
        cost = f" ${s['cost_usd']:.6f}" if s["cost_usd"] else ""
        model = f" [{s['model']}]" if s["model"] else ""
        print(f"  {ind}{mark} {s['name']:<22} {s['duration_ms']:>5}ms{model}{cost}")


# ------------------------------------------------------- canary data seeding
def assign_version(claim_id: str) -> str:
    """
    Deterministic bucketing by claim_id hash. Deterministic, not random, so a
    given claim always lands in the same bucket -- otherwise a retry of the same
    claim could be served by a different prompt version and the comparison is
    contaminated.
    """
    import hashlib
    cfg = active_version()
    if not cfg.get("canary") or not cfg.get("canary_pct"):
        return cfg["active"]
    h = int(hashlib.sha256(claim_id.encode()).hexdigest()[:8], 16) % 100
    return cfg["canary"] if h < cfg["canary_pct"] else cfg["active"]


def run_canary_traffic(n: int = 50):
    """
    Generate tagged runs so compare_versions() has something to compare.
    v2 uses a longer system prompt -- more tokens, and in this mock it is
    slightly better on ambiguous claims. That is the trade the comparison
    is supposed to surface.
    """
    import p07_cost_router.main as p7
    from core.trace import run as trace_run, span as trace_span, call_llm
    p7.install_fixture()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    claims = json.load(open(os.path.join(root, "data", "claims.json")))[:n]

    PROMPTS = {
        "v1": p7.REASON_SYSTEM,
        "v2": p7.REASON_SYSTEM + (
            " Consider policy exclusions, the notice period, and any prior "
            "claim history before concluding. State the controlling clause."),
    }
    counts = {}
    for c in claims:
        v = assign_version(c["claim_id"])
        counts[v] = counts.get(v, 0) + 1
        with trace_run("p11_canary", claim_id=c["claim_id"],
                       meta={"prompt_version": v}):
            with trace_span("reason", kind="llm"):
                call_llm(f"CLAIM:\n{c['fnol_text']}", p7.STRONG,
                         system=PROMPTS[v], span_name=f"reason_{v}")
    return counts


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dashboard", action="store_true")
    ap.add_argument("--alerts", action="store_true")
    ap.add_argument("--trace", help="run_id to expand")
    ap.add_argument("--runs", action="store_true")
    ap.add_argument("--canary", nargs=2, metavar=("A", "B"))
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--seed-canary", action="store_true",
                    help="split traffic 50/50 across v1/v2 and record tagged runs")
    a = ap.parse_args()

    if a.seed_canary:
        set_version(active="v1", canary="v2", canary_pct=50)
        counts = run_canary_traffic(50)
        print("traffic split:", counts)
        print(json.dumps(compare_versions("v1", "v2"), indent=2))

    elif a.dashboard:
        d = dashboard()
        print(f"\n{'='*72}\nOBSERVABILITY DASHBOARD\n{'='*72}")
        print(f"total runs {d['totals']['runs']}   "
              f"total cost ${d['totals']['cost_usd'] or 0:.6f}\n")
        print(f"{'project':<28}{'runs':>6}{'fail':>6}{'paused':>8}"
              f"{'cost $':>12}{'avg ms':>9}")
        for r in d["by_project"]:
            print(f"{r['project']:<28}{r['runs']:>6}{r['failed'] or 0:>6}"
                  f"{r['paused'] or 0:>8}{r['cost_usd'] or 0:>12.6f}"
                  f"{r['avg_ms'] or 0:>9.1f}")
        print(f"\n{'model':<20}{'calls':>7}{'tok in':>9}{'tok out':>9}"
              f"{'cost $':>12}{'avg ms':>9}{'ok %':>7}")
        for r in d["by_model"]:
            print(f"{r['model']:<20}{r['calls']:>7}{r['tin']:>9}{r['tout']:>9}"
                  f"{r['cost_usd']:>12.6f}{r['avg_ms']:>9.1f}{r['success_pct']:>7}")
        print(f"\nslowest spans:")
        for s in d["slowest_spans"]:
            print(f"  {s['name']:<24}{s['duration_ms']:>6}ms  {s['run_id']}")

    elif a.alerts:
        al = check_alerts()
        print(f"\n{'='*72}\nALERTS ({len(al)})\n{'='*72}")
        if not al:
            print("  none firing")
        for x in al:
            print(f"  [{x.severity.upper():<8}] {x.rule:<22} {x.message}")

    elif a.trace:
        print(f"\nSPAN TREE {a.trace}")
        _fmt_tree(a.trace)

    elif a.runs:
        for r in runs(25):
            print(f"  {r['run_id']}  {r['project']:<26} {r['status']:<8} "
                  f"${r['cost_usd']:.6f}  {r['claim_id']}")

    elif a.canary:
        print(json.dumps(compare_versions(*a.canary), indent=2))

    elif a.rollback:
        print(json.dumps(rollback(), indent=2))
    else:
        ap.print_help()
