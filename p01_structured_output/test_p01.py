"""
Tests for P1. Each failure mode gets its own test, because "it works on the
happy path" is not evidence for a component whose entire job is failure handling.
Run: python3 -m pytest p01_structured_output/test_p01.py -v
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from pydantic import ValidationError
from p01_structured_output.main import ClaimRecord, extract_json, build_repair_prompt

VALID = dict(claim_id="CLM-2026-0001", claimant_name="A B", peril="fire",
             amount_usd=1200.0, policy_number="POL-123456",
             loss_date="2026-03-01", injuries_reported=False,
             extraction_confidence=0.9)


# ---- schema contract ------------------------------------------------------
def test_valid_record_accepted():
    assert ClaimRecord(**VALID).amount_usd == 1200.0


def test_negative_amount_rejected():
    with pytest.raises(ValidationError):
        ClaimRecord(**dict(VALID, amount_usd=-5))


def test_absurd_amount_rejected():
    """Guards against a misplaced decimal becoming a $9bn payout."""
    with pytest.raises(ValidationError):
        ClaimRecord(**dict(VALID, amount_usd=9_000_000_000))


def test_bad_policy_format_rejected():
    with pytest.raises(ValidationError):
        ClaimRecord(**dict(VALID, policy_number="POLICY 12345"))


def test_null_policy_allowed():
    """Missing is legal; malformed is not. Messy FNOLs genuinely lack it."""
    assert ClaimRecord(**dict(VALID, policy_number=None)).policy_number is None


def test_bad_date_rejected():
    with pytest.raises(ValidationError):
        ClaimRecord(**dict(VALID, loss_date="03/01/2026"))


def test_unknown_peril_rejected():
    with pytest.raises(ValidationError):
        ClaimRecord(**dict(VALID, peril="water-damage"))


def test_confidence_bounds():
    with pytest.raises(ValidationError):
        ClaimRecord(**dict(VALID, extraction_confidence=1.4))


# ---- JSON extraction ------------------------------------------------------
def test_strips_markdown_fence():
    assert json.loads(extract_json('```json\n{"a": 1}\n```'))["a"] == 1


def test_strips_prose_preamble():
    assert json.loads(extract_json('Here you go:\n\n{"a": 1}'))["a"] == 1


def test_handles_nested_braces():
    """A regex-based extractor fails this one; the brace scanner does not."""
    src = 'text {"a": {"b": {"c": 2}}, "d": 3} trailing'
    assert json.loads(extract_json(src))["a"]["b"]["c"] == 2


def test_truncated_json_raises_not_silently_passes():
    with pytest.raises(json.JSONDecodeError):
        json.loads(extract_json('{"a": 1, "b": '))


# ---- repair prompt --------------------------------------------------------
def test_repair_prompt_carries_validator_error():
    """The specific error text is the payload -- without it, repair is a coin flip."""
    p = build_repair_prompt("orig text", '{"amount_usd": "$1,200"}',
                            "amount_usd: Input should be a valid number")
    assert "Input should be a valid number" in p
    assert "orig text" in p


# ---- end to end -----------------------------------------------------------
def test_pipeline_recovers_and_never_raises():
    from p01_structured_output.main import install_fixture, run_one
    install_fixture()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    claims = json.load(open(os.path.join(root, "data", "claims.json")))
    for c in claims[:10]:
        r = run_one(c)
        assert set(r) == {"ok", "record", "attempts", "failures"}
        assert r["attempts"] <= 3
        if r["ok"]:
            ClaimRecord(**r["record"])       # revalidate what we handed back
