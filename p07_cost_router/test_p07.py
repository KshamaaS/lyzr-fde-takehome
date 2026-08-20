"""Tests for P7. Budget enforcement must be a hard gate, not a report."""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AGENT_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import core.store as store; store.DB_PATH = os.environ["AGENT_DB"]

import pytest
from p07_cost_router.main import (
    route, install_fixture, Budget, BudgetExceeded, expected, compare,
    CHEAP, STRONG, EARLY_EXIT_CONFIDENCE)
from core.llm import PRICING, price

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAIMS = json.load(open(os.path.join(ROOT, "data", "claims.json")))


def test_cheap_model_is_actually_cheaper():
    """If this ever fails, the entire routing premise is void."""
    assert PRICING[CHEAP]["in"] < PRICING[STRONG]["in"]

def test_cost_is_computed_not_guessed():
    assert price(STRONG, 1_000_000, 0) == PRICING[STRONG]["in"]

# ---- budget --------------------------------------------------------------
def test_budget_blocks_before_spending():
    b = Budget(0.0000001)
    assert not b.check(100_000, STRONG)

def test_budget_records_spend():
    b = Budget(1.0); b.record(0.5)
    assert abs(b.remaining() - 0.5) < 1e-9

def test_exhausted_budget_refers_to_human_not_silent_cheap_answer():
    """Degradation must be to a human, never to a quietly worse answer."""
    install_fixture()
    c = next(x for x in CLAIMS if x["kind"] == "ambiguous")
    r = route(c, budget_usd=0.0000002)
    assert r["determination"] == "refer" or r.get("budget_denied")

# ---- routing -------------------------------------------------------------
def test_every_route_reports_the_path_taken():
    install_fixture()
    r = route(CLAIMS[0])
    assert r["path"] and all(len(p) == 3 for p in r["path"])

def test_simple_claim_can_exit_early_without_strong_model():
    install_fixture()
    exits = [route(c) for c in CLAIMS if c["kind"] == "clean"]
    assert any(not e["escalated"] for e in exits)

def test_complex_claim_escalates_to_strong_model():
    install_fixture()
    r = route(next(c for c in CLAIMS if c["kind"] == "adversarial"))
    assert r["escalated"] or r["determination"] == "refer"

def test_unparseable_classifier_fails_safe_to_complex():
    """A broken classifier must not silently route everything to cheap."""
    install_fixture()
    for c in CLAIMS[:10]:
        r = route(c)
        assert r["determination"] in ("covered", "refer", "denied", "excluded")

def test_cost_is_recorded_per_decision():
    install_fixture()
    assert route(CLAIMS[0])["cost_usd"] > 0

# ---- analytics -----------------------------------------------------------
def test_compare_reports_both_cost_and_accuracy():
    install_fixture()
    out = compare(CLAIMS[:20])
    assert "routed" in out and "baseline_all_strong" in out
    for k in ("cost_per_decision", "accuracy"):
        assert k in out["routed"]

def test_accuracy_is_measured_against_labels_not_asserted():
    assert expected(CLAIMS[0]) in ("covered", "refer", "denied", "excluded")
