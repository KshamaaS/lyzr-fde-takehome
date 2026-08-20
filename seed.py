"""Populate agent.db so the Space has data on first load."""
import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PROVIDER", "mock")

import p01_structured_output.main as p1
import p02_rag_citations.main as p2
import p06_hitl_approval.main as p6
import p07_cost_router.main as p7
import p08_event_automation.main as p8
import p11_observability.main as p11

p1.install_fixture(); p2.install_fixture(); p7.install_fixture()
claims = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "data", "claims.json")))

for c in claims[:30]:
    r = p1.run_one(c)
    if r["ok"]:
        gt = c["ground_truth"]
        p6.process_claim(c, r["record"], r["record"]["extraction_confidence"],
                         fraud_signal=gt["fraud_signal"], coverage=gt["coverage"],
                         reasoning="Automated triage determination.",
                         citations=["HO3_SEC_A_WATER:SEC-1"])
for c in claims[:20]:
    p7.route(c)
for q in p2.QUESTIONS:
    p2.ask(q)
for c in claims[:5]:
    p8.receive({"claim_id": c["claim_id"], "fnol_text": c["fnol_text"],
                "delivery_id": "seed"})
p8.drain()
p11.set_version("v1", "v2", 50)
p11.run_canary_traffic(40)
print("seeded")
