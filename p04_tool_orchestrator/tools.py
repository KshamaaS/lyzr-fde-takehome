"""
Tool implementations. Registered at import time into core.registry.

Deterministic and seeded from claim_id so runs are reproducible. In a real
deployment these are HTTP calls to the carrier's coverage system, an SIU fraud
service, and the payment rail.
"""
from __future__ import annotations
import sys, os, json, hashlib, random, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import BaseModel, Field
from core.registry import registry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLAIMS = None


def _claims():
    global _CLAIMS
    if _CLAIMS is None:
        _CLAIMS = {c["claim_id"]: c for c in
                   json.load(open(os.path.join(ROOT, "data", "claims.json")))}
    return _CLAIMS


def _rng(claim_id, salt=""):
    return random.Random(int(hashlib.sha256((claim_id + salt).encode())
                             .hexdigest()[:8], 16))


# ---- return schemas: the tool boundary is typed, same as the LLM boundary ----
class CoverageOut(BaseModel):
    claim_id: str
    status: str                                  # covered | excluded | ambiguous
    clause: str
    confidence: float = Field(ge=0, le=1)


class FraudOut(BaseModel):
    claim_id: str
    score: float = Field(ge=0, le=1)
    signals: list[str]
    recommendation: str                          # proceed | review | deny


class HistoryOut(BaseModel):
    claim_id: str
    prior_claims_24mo: int
    total_paid_usd: float
    same_address_claims: int


class PaymentOut(BaseModel):
    claim_id: str
    payment_id: str
    amount_usd: float
    status: str


@registry.register(capabilities=["coverage", "lookup"], est_cost_usd=0.002,
                   returns=CoverageOut)
def coverage_lookup(claim_id: str, **kw):
    """Determine coverage status for a claim against the policy."""
    time.sleep(0.04)
    c = _claims().get(claim_id)
    cov = (c or {}).get("ground_truth", {}).get("coverage", "unknown")
    clause = {"covered": "HO3:SEC-2", "ambiguous": "HO3:SEC-4",
              "disputed": "HO3:SEC-6", "unknown": "HO3:SEC-1"}.get(cov, "HO3:SEC-1")
    return {"claim_id": claim_id,
            "status": {"covered": "covered", "ambiguous": "ambiguous",
                       "disputed": "ambiguous", "unknown": "ambiguous"}[cov],
            "clause": clause,
            "confidence": 0.92 if cov == "covered" else 0.48}


@registry.register(capabilities=["fraud", "risk"], est_cost_usd=0.01,
                   returns=FraudOut)
def fraud_score(claim_id: str, **kw):
    """Return an SIU fraud risk score and contributing signals."""
    time.sleep(0.06)
    c = _claims().get(claim_id)
    r = _rng(claim_id, "fraud")
    flagged = (c or {}).get("ground_truth", {}).get("fraud_signal", False)
    score = round(r.uniform(0.72, 0.95) if flagged else r.uniform(0.02, 0.28), 2)
    signals = (["late notice", "repeat address", "policy recently reinstated"]
               if flagged else [])
    return {"claim_id": claim_id, "score": score, "signals": signals,
            "recommendation": "deny" if score > 0.8
                              else "review" if score > 0.5 else "proceed"}


@registry.register(capabilities=["history", "lookup"], est_cost_usd=0.001,
                   returns=HistoryOut)
def claim_history(claim_id: str, **kw):
    """Prior claim activity for the claimant and the insured address."""
    time.sleep(0.03)
    c = _claims().get(claim_id)
    r = _rng(claim_id, "hist")
    flagged = (c or {}).get("ground_truth", {}).get("fraud_signal", False)
    n = r.randint(2, 4) if flagged else r.randint(0, 1)
    return {"claim_id": claim_id, "prior_claims_24mo": n,
            "total_paid_usd": round(n * r.uniform(1200, 9000), 2),
            "same_address_claims": n}


@registry.register(capabilities=["payment", "disbursement"],
                   required_role="adjuster", mutating=True, est_cost_usd=0.0,
                   returns=PaymentOut)
def issue_payment(claim_id: str, amount_usd: float = 0.0, **kw):
    """Issue a payment against a claim. MUTATING - requires adjuster role."""
    return {"claim_id": claim_id, "payment_id": f"PAY-{claim_id[-4:]}",
            "amount_usd": float(amount_usd), "status": "issued"}


@registry.register(capabilities=["documents", "lookup"], est_cost_usd=0.0005)
def fetch_documents(claim_id: str, **kw):
    """Retrieve documents attached to the claim."""
    time.sleep(0.05)     # slowest tool -- makes parallel fan-out visible
    r = _rng(claim_id, "doc")
    return {"claim_id": claim_id,
            "documents": r.sample(["contractor_estimate.pdf", "photos.zip",
                                   "police_report.pdf", "policy_decl.pdf"],
                                  k=r.randint(1, 3))}


@registry.register(capabilities=["fraud", "risk"], est_cost_usd=0.008,
                   reliability=0.6)
def flaky_vendor_check(claim_id: str, **kw):
    """Third-party risk vendor. Fails ~40% of the time by design."""
    if _rng(claim_id, "flaky").random() < 0.4:
        raise RuntimeError("vendor gateway 503")
    return {"claim_id": claim_id, "vendor_flag": False}
