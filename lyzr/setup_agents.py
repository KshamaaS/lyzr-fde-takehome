"""
Creates the Lyzr-side assets: one Knowledge Base over the policy corpus, and
two agents that mirror the patterns a hosted agent can actually express.

Run:  LYZR_API_KEY=... python3 lyzr/setup_agents.py

Writes lyzr/agent_ids.json, which core/llm.py:LyzrProvider and
p02_rag_citations:LyzrRetriever read.

WHY ONLY TWO AGENTS: P2 (knowledge retrieval) and P3 (triage) are the two
patterns whose control flow fits inside a single agent.run() call. P1, P6, P7
need code between the model call and the response and cannot be expressed as a
hosted agent -- see PLATFORM_NOTES.md.
"""
import os, sys, json, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_ids.json")

POLICY_QA_PROMPT = """You are a claims policy assistant for a P&C carrier.

Answer coverage questions using ONLY the attached policy knowledge base.
Every sentence of your answer MUST end with a citation naming the clause it
came from, in square brackets, e.g. [HO3_SEC_A_WATER:SEC-1].

If the knowledge base does not contain the answer, reply exactly: INSUFFICIENT.
Do not answer from general insurance knowledge. A wrong coverage answer costs
more than no answer.

Return only JSON:
{"answer": "...", "citations": ["..."], "confidence": 0.0}"""

TRIAGE_PROMPT = """You are a first-line claims triage assistant.

Given a claim, decide what evidence is needed and in what order: coverage
status, fraud indicators, and prior claim history.

Rules you must not violate:
- Never state a settlement amount. Reserve setting is not your decision.
- If coverage is ambiguous or a fraud indicator is present, recommend
  escalation to a human adjuster regardless of claim value.
- If you cannot determine something from the evidence available, say so
  explicitly rather than inferring it.

Return only JSON:
{"needed_evidence": ["..."], "recommendation": "settle|refer|deny",
 "confidence": 0.0, "reasoning": "..."}"""


def main():
    key = os.environ.get("LYZR_API_KEY")
    if not key:
        sys.exit("LYZR_API_KEY not set. Get one at https://studio.lyzr.ai")

    try:
        from lyzr_adk.adk import Adk
    except ImportError:
        sys.exit("pip install lyzr-adk")

    adk = Adk(api_key=key)
    ids = {}

    # --- knowledge base -----------------------------------------------------
    print("creating knowledge base...")
    kb = adk.knowledge_base.create(
        name="claims-policy-corpus",
        description="HO-3 policy sections and claims authority matrix")
    ids["kb_id"] = kb["id"] if isinstance(kb, dict) else kb

    docs = sorted(glob.glob(os.path.join(ROOT, "data", "policy_docs", "*.md")))
    for path in docs:
        doc_id = os.path.basename(path).replace(".md", "")
        print(f"  uploading {doc_id}")
        adk.knowledge_base.add_document(
            kb_id=ids["kb_id"], file_path=path,
            metadata={"doc_id": doc_id, "source": "HO3_policy"})
    print(f"  {len(docs)} documents uploaded")

    # --- agents -------------------------------------------------------------
    for name, prompt, attach_kb in (
            ("claims-policy-qa", POLICY_QA_PROMPT, True),
            ("claims-triage", TRIAGE_PROMPT, False)):
        print(f"creating agent {name}...")
        kw = dict(name=name, system_prompt=prompt,
                  model="claude-sonnet-4-6", temperature=0.0)
        if attach_kb:
            kw["knowledge_base_id"] = ids["kb_id"]
        a = adk.agent.create(**kw)
        ids[name] = a["id"] if isinstance(a, dict) else a

    json.dump(ids, open(OUT, "w"), indent=2)
    print(f"\nwrote {OUT}")
    print(json.dumps(ids, indent=2))
    print("\nNow run:")
    print("  PROVIDER=lyzr RETRIEVER=lyzr python3 p02_rag_citations/main.py --demo")
    print("\nFREE TIER WARNING: Community is 500 credits/month and LLM tokens")
    print("bill separately. Do NOT run --all or golden-set sweeps against")
    print("PROVIDER=lyzr. Use mock for evaluation, lyzr for demo runs.")


if __name__ == "__main__":
    main()
