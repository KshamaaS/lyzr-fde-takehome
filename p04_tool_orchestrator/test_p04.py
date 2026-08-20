"""
Tests for P4. Conflict resolution and permission scoping are the two things
that must be deterministic, so they get exhaustive coverage.
"""
import sys, os, asyncio, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AGENT_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import core.store as store; store.DB_PATH = os.environ["AGENT_DB"]

import pytest
from core.registry import registry, PermissionDenied, ToolError
import p04_tool_orchestrator.tools          # registers the tools
from p04_tool_orchestrator.main import resolve, plan_by_capability

CLEAN = dict(fraud={"recommendation": "proceed", "score": 0.1},
             coverage={"status": "covered"}, history={"same_address_claims": 0})


# ---- registry ------------------------------------------------------------
def test_tools_registered():
    assert len(registry.list()) >= 5

def test_capability_lookup_returns_tools_not_names():
    assert registry.by_capability("coverage")

def test_unknown_tool_raises():
    with pytest.raises(ToolError):
        registry.get("no_such_tool")

def test_mutating_tools_are_marked():
    """P6 depends on this flag to know what needs approval."""
    assert any(t.mutating for t in registry.list())

def test_manifest_excludes_the_callable():
    """What the model sees must not contain a live function reference."""
    assert "fn" not in registry.describe()


# ---- permission scoping --------------------------------------------------
def test_payment_denied_without_role():
    with pytest.raises(PermissionDenied):
        registry.call("issue_payment", role="intake",
                      claim_id="CLM-1", amount_usd=100)

def test_payment_allowed_with_role():
    out = registry.call("issue_payment", role="adjuster",
                        claim_id="CLM-1", amount_usd=100)
    assert out["ok"]

def test_permission_is_checked_before_execution():
    """The check must be a gate, not an audit of something that already ran."""
    before = registry.call("claim_history", role="system", claim_id="CLM-X")
    with pytest.raises(PermissionDenied):
        registry.call("issue_payment", role="viewer", claim_id="CLM-X", amount_usd=1)
    assert before["ok"]

def test_readonly_tools_need_no_role():
    assert registry.call("coverage_lookup", role="intake", claim_id="CLM-1")["ok"]


# ---- conflict resolution: deterministic, exhaustive ----------------------
def test_clean_claim_pays():
    assert resolve(**CLEAN).decision == "pay"

def test_fraud_deny_vetoes_covered_coverage():
    """The asymmetry: fraud beats coverage, never the reverse."""
    v = resolve(fraud={"recommendation": "deny"},
                coverage={"status": "covered"}, history={})
    assert v.decision == "refer" and v.rule == "fraud_deny_overrides_coverage"

def test_fraud_review_plus_repeat_address_refers():
    v = resolve(fraud={"recommendation": "review"},
                coverage={"status": "covered"},
                history={"same_address_claims": 3})
    assert v.decision == "refer"

def test_fraud_review_without_repeat_history_does_not_block_alone():
    v = resolve(fraud={"recommendation": "review"},
                coverage={"status": "covered"},
                history={"same_address_claims": 0})
    assert v.rule != "fraud_review_with_repeat_history"

def test_ambiguous_coverage_refers():
    assert resolve(fraud={"recommendation": "proceed"},
                   coverage={"status": "ambiguous"}, history={}).decision == "refer"

def test_excluded_coverage_denies():
    assert resolve(fraud={"recommendation": "proceed"},
                   coverage={"status": "excluded"}, history={}).decision == "deny"

def test_unmatched_combination_defaults_to_human_not_pay():
    """The critical safety property: no unknown state falls through to payment."""
    v = resolve(fraud={}, coverage={}, history={})
    assert v.decision == "refer" and v.rule == "no_rule_matched"

def test_missing_tool_output_never_pays():
    for bad in ({}, {"status": None}, {"status": "unknown"}):
        assert resolve({"recommendation": "proceed"}, bad, {}).decision != "pay"

def test_precedence_is_order_dependent_and_documented():
    """Fraud rules must precede coverage rules in the table."""
    from p04_tool_orchestrator.main import PRECEDENCE
    names = [n for n, _, _ in PRECEDENCE]
    assert names.index("fraud_deny_overrides_coverage") < names.index("clean_covered")


# ---- parallel execution --------------------------------------------------
def test_parallel_fanout_returns_all_results():
    calls = [{"tool": "coverage_lookup", "args": {"claim_id": "CLM-1"}},
             {"tool": "fraud_score", "args": {"claim_id": "CLM-1"}},
             {"tool": "claim_history", "args": {"claim_id": "CLM-1"}}]
    out = asyncio.run(registry.call_many(calls, role="adjuster"))
    assert len(out) == 3

def test_one_failing_tool_does_not_cancel_the_others():
    calls = [{"tool": "coverage_lookup", "args": {"claim_id": "CLM-1"}},
             {"tool": "issue_payment", "args": {"claim_id": "CLM-1", "amount_usd": 1}}]
    out = asyncio.run(registry.call_many(calls, role="intake"))   # payment denied
    assert out[0]["ok"] and not out[1]["ok"]

def test_plan_by_capability_resolves_to_concrete_tools():
    plan = plan_by_capability(["coverage", "fraud"])
    assert all("tool" in p for p in plan)
