"""
P1 - Structured Output Agent
Extract a validated ClaimRecord from free-text FNOL.

The whole point: an LLM returns a string. A downstream system needs a typed
record. Everything between those two facts is this file.

Control flow is ours, not the model's:
    call -> extract JSON -> validate against schema
         -> on failure: repair prompt carrying the EXACT validator error
         -> retry up to MAX_ATTEMPTS -> hard fail, logged, never silent
"""
from __future__ import annotations
import sys, os, json, re, argparse
from typing import Optional, Literal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import BaseModel, Field, ValidationError, field_validator
from core.trace import run, span, call_llm
from core.store import db, audit
from core.llm import get_provider

MAX_ATTEMPTS = 3          # justified in README from measured data
MODEL = os.environ.get("P1_MODEL", "mock-cheap")

PERILS = ["water damage", "fire", "theft", "windstorm", "hail",
          "vehicle impact", "vandalism", "frozen pipe", "smoke",
          "falling object", "unknown"]


class ClaimRecord(BaseModel):
    """The contract. If it doesn't validate, it doesn't leave this module."""
    claim_id: str
    claimant_name: Optional[str] = None
    peril: Literal[tuple(PERILS)] = "unknown"          # type: ignore
    amount_usd: float = Field(ge=0, le=10_000_000)
    policy_number: Optional[str] = None
    loss_date: Optional[str] = None
    injuries_reported: bool = False
    extraction_confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("policy_number")
    @classmethod
    def policy_format(cls, v):
        if v is None:
            return v
        if not re.fullmatch(r"POL-\d{6}", v):
            raise ValueError("policy_number must match POL-###### or be null")
        return v

    @field_validator("loss_date")
    @classmethod
    def iso_date(cls, v):
        if v is None:
            return v
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("loss_date must be ISO YYYY-MM-DD or null")
        return v


SYSTEM = (
    "P1_EXTRACT. You extract insurance claim data. Return ONLY a JSON object "
    "matching the schema. No prose, no markdown fences.\n"
    f"Fields: claim_id(str), claimant_name(str|null), peril(one of {PERILS}), "
    "amount_usd(number>=0), policy_number('POL-######'|null), "
    "loss_date('YYYY-MM-DD'|null), injuries_reported(bool), "
    "extraction_confidence(0..1). Set confidence low when the source is vague."
)


def extract_json(text: str) -> str:
    """
    Models wrap JSON in prose and fences even when told not to. Strip it.
    Balanced-brace scan, not a regex -- regex breaks on nested objects.
    """
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = t.find("{")
    if start == -1:
        return t
    depth = 0
    for i, ch in enumerate(t[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return t[start:i + 1]
    return t[start:]


def build_repair_prompt(original: str, bad_output: str, error: str) -> str:
    """
    The repair prompt carries the validator's exact error text.
    Telling the model 'that was invalid' fixes little; telling it
    'amount_usd: Input should be a valid number' fixes most of it.
    """
    return (
        f"Your previous output was rejected by schema validation.\n\n"
        f"YOUR OUTPUT:\n{bad_output[:600]}\n\n"
        f"VALIDATOR ERRORS:\n{error}\n\n"
        f"Fix ONLY those errors. Return the corrected JSON object and nothing else.\n\n"
        f"ORIGINAL SOURCE TEXT:\n{original}"
    )


def parse_claim(claim_id: str, fnol_text: str) -> dict:
    """
    Returns {ok, record|None, attempts, failures[]}.
    Never raises on model misbehaviour -- a bad claim is data, not an exception.
    """
    prompt = f"Claim ID is {claim_id}.\n\nFNOL TEXT:\n{fnol_text}"
    failures, last_out = [], ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        with span(f"attempt_{attempt}", kind="logic") as sp:
            p = prompt if attempt == 1 else build_repair_prompt(
                fnol_text, last_out, failures[-1]["error"])

            resp = call_llm(p, MODEL, system=SYSTEM,
                            span_name=f"extract_a{attempt}", attempt=attempt)
            last_out = resp.text

            try:
                candidate = json.loads(extract_json(resp.text))
            except json.JSONDecodeError as e:
                err = f"not valid JSON: {e}"
                failures.append({"attempt": attempt, "stage": "json", "error": err})
                sp["attrs"].update(outcome="json_error")
                continue

            try:
                rec = ClaimRecord(**candidate)
            except ValidationError as e:
                err = "; ".join(
                    f"{'.'.join(str(x) for x in d['loc'])}: {d['msg']}"
                    for d in e.errors())
                failures.append({"attempt": attempt, "stage": "schema", "error": err})
                sp["attrs"].update(outcome="schema_error", detail=err)
                continue

            sp["attrs"].update(outcome="valid")
            return {"ok": True, "record": rec.model_dump(),
                    "attempts": attempt, "failures": failures}

    # Exhausted. Log loudly. Downstream gets a typed failure, not a guess.
    audit("p01", "extraction_failed", claim_id=claim_id,
          detail={"attempts": MAX_ATTEMPTS, "failures": failures})
    return {"ok": False, "record": None,
            "attempts": MAX_ATTEMPTS, "failures": failures}


def run_one(claim: dict) -> dict:
    with run("p01_structured_output", claim_id=claim["claim_id"]):
        return parse_claim(claim["claim_id"], claim["fnol_text"])


# ---------------------------------------------------------------- mock fixture
def install_fixture():
    """
    Simulates a real model for offline runs. Failure modes are the ones that
    actually occur in production, at rates that make the repair loop matter:
      attempt 1: 40% chance of a defect (fence-wrapped / truncated / wrong type)
      attempt 2+: defect rate drops -- the repair prompt is doing work
    """
    prov = get_provider()
    if prov.name != "mock":
        return
    claims = {c["claim_id"]: c for c in json.load(
        open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "claims.json")))}

    def gen(prompt, rng, attempt):
        m = re.search(r"CLM-2026-\d{4}", prompt)
        c = claims.get(m.group(0)) if m else None
        gt = c["ground_truth"] if c else {}
        messy = c and c["kind"] == "messy"

        pol = re.search(r"POL-\d{6}", c["fnol_text"]) if c else None
        good = {
            "claim_id": m.group(0) if m else "CLM-0000",
            "claimant_name": (c["fnol_text"].split("Policyholder ")[-1].split(" reported")[0]
                              if c and "Policyholder " in c["fnol_text"] else None),
            "peril": gt.get("peril", "unknown"),
            "amount_usd": gt.get("amount_usd", 0),
            "policy_number": pol.group(0) if pol else None,
            "loss_date": None,
            "injuries_reported": False,
            "extraction_confidence": 0.42 if messy else round(rng.uniform(0.82, 0.97), 2),
        }

        defect_rate = 0.40 if attempt == 1 else (0.25 if attempt == 2 else 0.05)
        if messy:
            defect_rate += 0.25
        if rng.random() > defect_rate:
            return json.dumps(good)

        # pick a realistic defect
        mode = rng.choice(["fence", "truncate", "type", "policy", "prose", "enum"])
        if mode == "fence":
            return "```json\n" + json.dumps(good) + "\n```"
        if mode == "truncate":
            return json.dumps(good)[: int(len(json.dumps(good)) * 0.7)]
        if mode == "type":
            bad = dict(good, amount_usd=f"${good['amount_usd']:,.2f}")
            return json.dumps(bad)
        if mode == "policy":
            return json.dumps(dict(good, policy_number="POLICY 12345"))
        if mode == "enum":
            return json.dumps(dict(good, peril="water-damage"))
        return "Here is the extracted claim data:\n\n" + json.dumps(good)

    prov.register("P1_EXTRACT", gen)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--claim", help="single claim id")
    ap.add_argument("--all", action="store_true", help="run the 50-claim golden set")
    a = ap.parse_args()

    install_fixture()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    claims = json.load(open(os.path.join(root, "data", "claims.json")))

    if a.claim:
        c = next(x for x in claims if x["claim_id"] == a.claim)
        r = run_one(c)
        print(json.dumps(r, indent=2))
    elif a.all:
        from collections import Counter
        recovered = Counter()
        ok = 0
        stages = Counter()
        for c in claims:
            r = run_one(c)
            if r["ok"]:
                ok += 1
                recovered[r["attempts"]] += 1
            for f in r["failures"]:
                stages[f["stage"]] += 1
        print(f"\n{'='*54}\nP1 GOLDEN SET  (n={len(claims)}, MAX_ATTEMPTS={MAX_ATTEMPTS})\n{'='*54}")
        print(f"parsed successfully : {ok}/{len(claims)}  ({ok/len(claims)*100:.0f}%)")
        cum = 0
        for k in sorted(recovered):
            cum += recovered[k]
            print(f"  succeeded on attempt {k}: {recovered[k]:2d}   cumulative {cum/len(claims)*100:5.1f}%")
        print(f"hard failures       : {len(claims)-ok}")
        print(f"failure stages      : {dict(stages)}")
    else:
        ap.print_help()
