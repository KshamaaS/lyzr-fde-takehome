"""
Control plane. Three tabs over the shared SQLite that every project writes to.

Architectural rule: this file READS. It never imports a project's agent logic
except P6's decision functions, because approving a claim is a write the human
performs. That one seam is why one dashboard covers eight projects without
coupling any of them together.

Run locally:  streamlit run app/streamlit_app.py
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
import pandas as pd

# --- boot seed -------------------------------------------------------------
# Streamlit Community Cloud gives an ephemeral filesystem, so agent.db does not
# survive a restart. Seed on first boot if the DB is empty. Cached so it runs
# once per container, not once per rerun.
@st.cache_resource
def _bootstrap():
    import subprocess, pathlib
    root = pathlib.Path(__file__).parent.parent
    db = root / "data" / "agent.db"
    claims = root / "data" / "claims.json"
    if not claims.exists():
        subprocess.run([sys.executable, "data/make_claims.py"], cwd=root, check=False)
    if not db.exists() or db.stat().st_size < 20_000:
        subprocess.run([sys.executable, "seed.py"], cwd=root, check=False)
    return True

os.environ.setdefault("PROVIDER", "mock")
_bootstrap()

# --- boot guard -------------------------------------------------------------
# Streamlit Community Cloud gives a writable but EPHEMERAL filesystem, and
# data/agent.db is gitignored (traces are regenerated, never committed). So the
# app seeds itself on first boot if the database is empty. Idempotent: a warm
# container skips it.
import os as _os, subprocess as _sp, pathlib as _pl
_os.environ.setdefault("PROVIDER", "mock")
_ROOT = _pl.Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "agent.db"
if not _DB.exists() or _DB.stat().st_size < 20_000:
    if not (_ROOT / "data" / "claims.json").exists():
        _sp.run([sys.executable, "data/make_claims.py"], cwd=_ROOT, check=False)
    _sp.run([sys.executable, "seed.py"], cwd=_ROOT, check=False)

from core.store import db
from p11_observability.main import (dashboard, span_tree, check_alerts, runs,
                                    active_version, rollback)
from p06_hitl_approval.main import (pending, decide, resume, override_rate,
                                    expire_stale, audit_trail,
                                    AUTO_APPROVE_MAX_USD, MIN_CONFIDENCE)

st.set_page_config(page_title="Claims Agent Control Plane",
                   page_icon="◆", layout="wide")

st.markdown("""
<style>
  /* Theme is pinned in .streamlit/config.toml; these only refine it.
     Deliberately no background or text-colour overrides here -- hardcoding a
     light background while Streamlit picks text colour from the viewer's
     dark-mode preference is what produced white-on-white. */
  h1,h2,h3 { font-family: ui-sans-serif, system-ui; letter-spacing:-.02em;
             color:#18181b; }
  [data-testid="stMetricValue"] { font-size:1.5rem; color:#18181b; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
          font-size:.82rem; color:#18181b; }
  code { color:#166534 !important; background:#f4f4f5 !important; }
  .stMarkdown, .stMarkdown p, .stMarkdown li { color:#18181b; }
  [data-testid="stExpander"] summary { color:#18181b !important; }
  [data-testid="stExpander"] summary p { color:#18181b !important; }
  .stDataFrame, .stTable { color:#18181b; }
</style>""", unsafe_allow_html=True)

st.title("Claims Agent Control Plane")
st.caption("Insurance claims operations — 8 agentic patterns over one runtime. "
           "Every number below is read from `data/agent.db`, written by the "
           "agents as they ran.")

DB_EXISTS = os.path.exists(os.environ.get("AGENT_DB", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "agent.db")))

if not DB_EXISTS:
    st.warning("No run data yet. Run `python3 pipeline.py --all` first.")
    st.stop()

tab_runs, tab_appr, tab_cost = st.tabs(
    ["Runs & Traces", "Approval Queue", "Cost & Routing"])

# ══════════════════════════════════════════════════════════ RUNS
with tab_runs:
    d = dashboard()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total runs", d["totals"]["runs"])
    c2.metric("Total cost", f"${d['totals']['cost_usd'] or 0:.4f}")
    alerts = check_alerts()
    c3.metric("Alerts firing", len(alerts))
    crit = sum(1 for a in alerts if a.severity == "critical")
    c4.metric("Critical", crit)

    if alerts:
        with st.expander(f"Alerts ({len(alerts)})", expanded=crit > 0):
            for a in alerts[:20]:
                icon = {"critical": "🔴", "warning": "🟡"}.get(a.severity, "🔵")
                st.markdown(f"{icon} **{a.rule}** — {a.message}")

    st.subheader("By project")
    st.dataframe(pd.DataFrame(d["by_project"]), use_container_width=True,
                 hide_index=True)

    st.subheader("Span trace")
    rr = runs(200)
    if rr:
        labels = {f"{r['project']} · {r['claim_id']} · {r['status']} "
                  f"· ${r['cost_usd']:.5f}": r["run_id"] for r in rr}
        pick = st.selectbox("Select a run", list(labels))
        rid = labels[pick]
        tree = span_tree(rid)
        rows = []
        for s in tree:
            rows.append({
                "": "  " * s["depth"] + ("✓" if s["ok"] else "✗"),
                "span": "  " * s["depth"] + s["name"],
                "kind": s["kind"], "ms": s["duration_ms"],
                "model": s["model"] or "", "tok_in": s["tokens_in"],
                "tok_out": s["tokens_out"],
                "cost": f"${s['cost_usd']:.6f}" if s["cost_usd"] else "",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True,
                     hide_index=True, height=min(420, 40 + 35 * len(rows)))
        with st.expander("Span attributes"):
            for s in tree:
                a = json.loads(s["attrs"] or "{}")
                if a:
                    st.markdown(f"**{s['name']}** — `{json.dumps(a)[:400]}`")

    st.subheader("Deployment version")
    v = active_version()
    vc1, vc2 = st.columns([3, 1])
    vc1.json(v)
    if vc2.button("Rollback canary", use_container_width=True):
        rollback()
        st.rerun()

# ══════════════════════════════════════════════════════════ APPROVALS
with tab_appr:
    st.caption(f"Gate thresholds — auto-approve below **${AUTO_APPROVE_MAX_USD:,.0f}** "
               f"and confidence at or above **{MIN_CONFIDENCE:.2f}**. "
               "Enforced in `evaluate_gate()`, not in a prompt.")

    m = override_rate()
    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Pending", m["counts"].get("pending", 0))
    a2.metric("Approved", m["counts"].get("approved", 0))
    a3.metric("Rejected", m["counts"].get("rejected", 0))
    a4.metric("Override rate",
              f"{m['override_rate']:.1%}" if m["override_rate"] is not None else "—",
              help="Below ~5% suggests the thresholds are miscalibrated: the "
                   "gate is pausing claims a human always waves through.")
    if m["override_rate"] is not None:
        st.caption(f"Interpretation: {m['interpretation']}")

    if st.button("Run SLA sweep (expire overdue)"):
        exp = expire_stale()
        st.info(f"Expired {len(exp)} approval(s) past their deadline.")
        st.rerun()

    q = pending()
    st.subheader(f"Queue — {len(q)} pending")
    if not q:
        st.success("Queue empty.")
    for p in q[:25]:
        reasons = json.loads(p["reason"])
        prop = json.loads(p["proposed"])
        age_h = (time.time() - p["created_at"]) / 3600
        ttl_h = (p["expires_at"] - time.time()) / 3600
        with st.expander(
                f"{p['claim_id']} — ${p['amount_usd']:,.2f} — "
                f"conf {p['confidence']:.2f} — {len(reasons)} reason(s)"
                + ("  ⚠️ OVERDUE" if ttl_h < 0 else f"  ({ttl_h:.0f}h left)")):
            l, r = st.columns([2, 1])
            with l:
                st.markdown("**Why this paused**")
                for x in reasons:
                    st.markdown(f"- {x}")
                st.markdown("**Agent reasoning**")
                st.write(prop.get("reasoning") or "_none supplied_")
                if prop.get("citations"):
                    st.markdown("**Policy citations**")
                    st.code(", ".join(prop["citations"]))
                st.markdown("**Extracted record**")
                st.json(prop.get("extracted", {}))
            with r:
                st.markdown("**Decision**")
                actor = st.text_input("Your ID", value="adjuster.demo",
                                      key=f"a{p['approval_id']}")
                note = st.text_area("Note", key=f"n{p['approval_id']}",
                                    height=80)
                if st.button("Approve", key=f"y{p['approval_id']}",
                             use_container_width=True):
                    decide(p["approval_id"], "approved", actor, note)
                    st.success(resume(p["approval_id"]))
                    st.rerun()
                if st.button("Reject", key=f"x{p['approval_id']}",
                             use_container_width=True):
                    decide(p["approval_id"], "rejected", actor, note)
                    st.rerun()

    st.subheader("Audit trail")
    cid = st.text_input("Claim ID", value=q[0]["claim_id"] if q else "")
    if cid:
        tr = audit_trail(cid)
        if tr:
            st.dataframe(pd.DataFrame([{
                "seq": e["seq"],
                "when": time.strftime("%H:%M:%S", time.localtime(e["ts"])),
                "actor": e["actor"], "action": e["action"],
                "detail": (e["detail"] or "")[:120]} for e in tr]),
                use_container_width=True, hide_index=True)
            st.caption("Append-only. A corrected decision is a new row, never "
                       "an edit — which is what makes this defensible to a "
                       "regulator.")
        else:
            st.info("No audit entries for that claim.")

# ══════════════════════════════════════════════════════════ COST
with tab_cost:
    d = dashboard()
    st.subheader("Spend by model")
    st.dataframe(pd.DataFrame(d["by_model"]), use_container_width=True,
                 hide_index=True)

    with db() as c:
        proj = pd.DataFrame([dict(r) for r in c.execute(
            "SELECT project, ROUND(SUM(cost_usd),6) cost FROM runs "
            "GROUP BY project ORDER BY cost DESC")])
    if not proj.empty:
        st.subheader("Spend by project")
        st.bar_chart(proj.set_index("project"))

    st.subheader("Routing outcome")
    with db() as c:
        ee = c.execute("SELECT COUNT(*) n FROM audit WHERE action='early_exit'"
                       ).fetchone()["n"]
        n7 = c.execute("SELECT COUNT(*) n FROM runs WHERE project='p07_cost_router'"
                       ).fetchone()["n"]
    k1, k2, k3 = st.columns(3)
    k1.metric("Router runs", n7)
    k2.metric("Early exits", ee)
    k3.metric("Early-exit rate", f"{ee/n7:.0%}" if n7 else "—")

    st.info(
        "**Measured finding.** On this 50-claim set, routing cost **14.5% more** "
        "than sending everything to the strong model — the set is deliberately "
        "hard (only 12/50 routine), so most claims pay for both the classifier "
        "and the reasoner. Routing breaks even near **30% routine traffic** and "
        "reaches the vendor-claimed 40–60% saving only above **80%**. "
        "Run `python3 p07_cost_router/main.py --mix` for the full table.")

    st.subheader("Slowest spans")
    st.dataframe(pd.DataFrame(d["slowest_spans"]), use_container_width=True,
                 hide_index=True)
