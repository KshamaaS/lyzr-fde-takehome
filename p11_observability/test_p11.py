"""Tests for P11. It is a read model, so it is tested against seeded runs."""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP = tempfile.mkdtemp()
os.environ["AGENT_DB"] = os.path.join(TMP, "t.db")
import core.store as store; store.DB_PATH = os.environ["AGENT_DB"]

import p11_observability.main as obs
obs.VERSION_FILE = os.path.join(TMP, "active_version.json")

from core.trace import run, span, call_llm
from p01_structured_output.main import install_fixture as p1fix, run_one
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAIMS = json.load(open(os.path.join(ROOT, "data", "claims.json")))


def _seed():
    p1fix()
    for c in CLAIMS[:5]:
        run_one(c)

def test_dashboard_aggregates_runs_written_by_other_projects():
    """P11 must not need cooperation from the projects it observes."""
    _seed()
    d = obs.dashboard()
    assert d["totals"]["runs"] >= 5
    assert any(r["project"] == "p01_structured_output" for r in d["by_project"])

def test_cost_rolls_up_from_spans():
    _seed()
    assert obs.dashboard()["totals"]["cost_usd"] > 0

def test_model_breakdown_reports_tokens():
    _seed()
    assert all("tin" in m for m in obs.dashboard()["by_model"])

def test_span_tree_reconstructs_a_run():
    _seed()
    rid = obs.runs(limit=1)[0]["run_id"]
    assert len(obs.span_tree(rid)) >= 1

def test_alerts_return_structured_objects():
    _seed()
    for a in obs.check_alerts():
        assert a.severity in ("info", "warning", "critical")

def test_version_defaults_to_v1():
    assert obs.active_version()["active"] == "v1"

def test_set_and_read_canary_config():
    obs.set_version("v1", "v2", 25)
    cfg = obs.active_version()
    assert cfg["canary"] == "v2" and cfg["canary_pct"] == 25

def test_bucketing_is_deterministic():
    """Same claim must always land in the same bucket, or A/B is contaminated."""
    obs.set_version("v1", "v2", 50)
    assert obs.assign_version("CLM-2026-0001") == obs.assign_version("CLM-2026-0001")

def test_bucketing_splits_traffic():
    obs.set_version("v1", "v2", 50)
    buckets = {obs.assign_version(c["claim_id"]) for c in CLAIMS}
    assert buckets == {"v1", "v2"}

def test_zero_pct_canary_sends_all_traffic_to_active():
    obs.set_version("v1", "v2", 0)
    assert all(obs.assign_version(c["claim_id"]) == "v1" for c in CLAIMS[:10])

def test_rollback_disables_canary():
    obs.set_version("v1", "v2", 50)
    assert obs.rollback()["canary"] is None

def test_rollback_is_audited():
    obs.set_version("v1", "v2", 50)
    obs.rollback()
    with store.db() as c:
        n = c.execute("SELECT COUNT(*) n FROM audit WHERE action='rollback'").fetchone()["n"]
    assert n >= 1
