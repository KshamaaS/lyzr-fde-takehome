"""
Generates 50 synthetic FNOL (First Notice of Loss) claims with ground-truth labels.

Every measured number in this repo -- P1 parse rate, P7 cost per decision,
P6 pause rate -- is computed against this set. It is seeded and checked in,
so any reviewer reproduces the same numbers.

Deliberate composition (not uniform random):
  - 12 "clean"      : well-formed, low value, should auto-approve
  - 12 "high_value" : above the $5k approval threshold -> must pause (P6)
  - 10 "messy"      : typos, missing fields, run-on prose -> stress P1
  - 8  "ambiguous"  : coverage genuinely unclear -> low confidence -> pause
  - 8  "adversarial": contradictory or fraud-signal claims -> P4 conflict path
"""
import json, random, os

random.seed(20260817)

FIRST = ["Maria", "James", "Priya", "Daniel", "Aisha", "Robert", "Chen", "Elena",
         "Marcus", "Sofia", "Ahmed", "Grace", "Tomas", "Nina", "Victor"]
LAST = ["Alvarez", "Okonkwo", "Nakamura", "Fitzgerald", "Haddad", "Lindqvist",
        "Rossi", "Petrov", "Mbeki", "Kowalski", "Silva", "Byrne"]
PERILS = ["water damage", "fire", "theft", "windstorm", "hail", "vehicle impact",
          "vandalism", "frozen pipe", "smoke", "falling object"]
STATES = ["NY", "NJ", "CT", "PA", "MA"]


def name():
    return f"{random.choice(FIRST)} {random.choice(LAST)}"


def clean(i):
    peril = random.choice(PERILS)
    amt = round(random.uniform(400, 4200), 2)
    return dict(
        text=(f"Policyholder {name()} reported {peril} at the insured property on "
              f"2026-0{random.randint(1,8)}-{random.randint(10,28)}. "
              f"Estimated damage ${amt:,.2f}. Policy POL-{random.randint(100000,999999)} "
              f"active. Contractor estimate attached. No injuries reported."),
        label=dict(peril=peril, amount_usd=amt, parseable=True,
                   expect_pause=False, coverage="covered", fraud_signal=False))


def high_value(i):
    peril = random.choice(["fire", "water damage", "windstorm"])
    amt = round(random.uniform(5200, 48000), 2)
    return dict(
        text=(f"Claimant {name()} reports major {peril} loss. Structural damage to "
              f"kitchen and two bedrooms. Adjuster preliminary estimate "
              f"${amt:,.2f}. Policy POL-{random.randint(100000,999999)}, "
              f"{random.choice(STATES)}. Temporary housing requested."),
        label=dict(peril=peril, amount_usd=amt, parseable=True,
                   expect_pause=True, coverage="covered", fraud_signal=False))


def messy(i):
    peril = random.choice(PERILS)
    amt = round(random.uniform(300, 6000), 2)
    variants = [
        f"cust called re {peril}... says approx {amt} maybe more?? no policy num handy will send",
        f"FWD: FW: RE: claim -- {peril} in basement. amount unclear. approx ${amt}. pls advise",
        f"{peril}\namt: {amt}\npolicy: ????\nnotes: caller upset, disconnected mid-call",
        f"went out to property. {peril}. damage bad. est {amt} usd. photos to follow. -adj",
    ]
    return dict(
        text=random.choice(variants),
        label=dict(peril=peril, amount_usd=amt, parseable=False,
                   expect_pause=True, coverage="unknown", fraud_signal=False))


def ambiguous(i):
    amt = round(random.uniform(1500, 9000), 2)
    return dict(
        text=(f"{name()} reports water damage originating from a burst pipe in an "
              f"unheated crawlspace during a cold snap. Unclear whether the pipe "
              f"froze due to lack of maintenance (excluded) or sudden temperature "
              f"drop (covered). Estimate ${amt:,.2f}. "
              f"Policy POL-{random.randint(100000,999999)}."),
        label=dict(peril="frozen pipe", amount_usd=amt, parseable=True,
                   expect_pause=True, coverage="ambiguous", fraud_signal=False))


def adversarial(i):
    amt = round(random.uniform(3000, 25000), 2)
    return dict(
        text=(f"{name()} reports theft of high-value items. Claim filed "
              f"{random.randint(31,89)} days after reported incident date. "
              f"Third claim in 14 months at same address. No police report "
              f"reference provided. Requested amount ${amt:,.2f}. "
              f"Policy POL-{random.randint(100000,999999)} reinstated 3 weeks "
              f"before loss date."),
        label=dict(peril="theft", amount_usd=amt, parseable=True,
                   expect_pause=True, coverage="disputed", fraud_signal=True))


PLAN = [("clean", clean, 12), ("high_value", high_value, 12),
        ("messy", messy, 10), ("ambiguous", ambiguous, 8),
        ("adversarial", adversarial, 8)]

claims, n = [], 0
for kind, fn, count in PLAN:
    for i in range(count):
        n += 1
        rec = fn(i)
        claims.append(dict(claim_id=f"CLM-2026-{n:04d}", kind=kind,
                           fnol_text=rec["text"], ground_truth=rec["label"]))

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claims.json")
with open(out, "w") as f:
    json.dump(claims, f, indent=2)

print(f"wrote {len(claims)} claims -> {out}")
for kind, _, c in PLAN:
    print(f"  {kind:12s} {c:2d}")
print(f"  expect_pause: {sum(1 for c in claims if c['ground_truth']['expect_pause'])}/50")
