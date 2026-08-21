"""
Provisions the Lyzr-side assets: one Knowledge Base over the policy corpus, and
two agents mirroring the patterns a hosted agent can actually express.

    LYZR_API_KEY=... python3 lyzr/setup_agents.py

Writes lyzr/agent_ids.json, read by core/llm.py:LyzrProvider and
p02_rag_citations:LyzrRetriever.

WHY ONLY TWO AGENTS
P2 (knowledge-grounded Q&A) and P3 (triage) are the two patterns whose control
flow fits inside a single Agent.run() call. P1, P6 and P7 need code between the
model call and the response. See PLATFORM_NOTES.md.
"""
import os, sys, json, glob, asyncio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_ids.json")

POLICY_QA_INSTRUCTIONS = """Answer coverage questions using ONLY the attached policy knowledge base.

Every sentence of your answer MUST end with a citation naming the clause it came
from, in square brackets, e.g. [HO3_SEC_A_WATER:SEC-1].

If the knowledge base does not contain the answer, reply exactly: INSUFFICIENT.
Do not answer from general insurance knowledge. A wrong coverage answer costs
more than no answer.

Return only JSON:
{"answer": "...", "citations": ["..."], "confidence": 0.0}"""

TRIAGE_INSTRUCTIONS = """Given a claim, decide what evidence is needed and in what order:
coverage status, fraud indicators, and prior claim history.

Rules you must not violate:
- Never state a settlement amount. Reserve setting is not your decision.
- If coverage is ambiguous or a fraud indicator is present, recommend escalation
  to a human adjuster regardless of claim value.
- If you cannot determine something from the evidence available, say so
  explicitly rather than inferring it.

Return only JSON:
{"needed_evidence": ["..."], "recommendation": "settle|refer|deny",
 "confidence": 0.0, "reasoning": "..."}"""


async def main():
    key = os.environ.get("LYZR_API_KEY")
    if not key:
        sys.exit("LYZR_API_KEY not set. Get one at https://studio.lyzr.ai")

    try:
        from lyzr import Studio
    except ImportError:
        sys.exit("pip install lyzr-adk   (import name is `lyzr`, not `lyzr_adk`)")

    studio = Studio(api_key=key)
    ids = {}

    try:
        # --- knowledge base -------------------------------------------------
        print("creating knowledge base...")
        kb = await studio.acreate_knowledge_base(
            # KB names are constrained to lowercase/digits/underscore -- the
            # SDK rejects hyphens at the Pydantic layer, not the API layer.
            name="claims_policy_corpus",
            description="HO-3 policy sections and claims settlement authority matrix",
            vector_store="qdrant",
            embedding_model="text-embedding-3-large")
        ids["kb_id"] = getattr(kb, "id", None) or kb.model_dump().get("id")
        print(f"  kb id: {ids['kb_id']}")

        # KnowledgeBase.add_text takes the content directly. The corpus is
        # markdown, so add_text is correct -- add_pdf/add_docx would need a
        # conversion step for no benefit.
        docs = sorted(glob.glob(os.path.join(ROOT, "data", "policy_docs", "*.md")))
        for path in docs:
            doc_id = os.path.basename(path).replace(".md", "")
            print(f"  adding {doc_id}")
            # `source` is the only per-document field the SDK accepts -- there
            # is no metadata dict. We put the doc_id there because P2's citation
            # contract needs a stable, clause-addressable identifier to come back
            # out of retrieval. chunk_size is left at the default: the corpus is
            # already split on section headers, which are the legally meaningful
            # boundaries.
            await kb.aadd_text(text=open(path).read(), source=doc_id)
        print(f"  {len(docs)} documents ingested")

        # --- agents ---------------------------------------------------------
        # NOTE: llm_credential_id defaults to 'lyzr_openai'. Anthropic requires
        # a credential configured in Studio; left at the default so this runs
        # out of the box on a fresh account.
        print("creating agent claims-policy-qa...")
        qa = await studio.acreate_agent(
            name="claims-policy-qa",
            provider="openai",
            role="Claims policy assistant for a P&C carrier",
            goal="Answer coverage questions with clause-level citations, or abstain",
            instructions=POLICY_QA_INSTRUCTIONS,
            temperature=0.0,
            store_messages=True)
        ids["claims-policy-qa"] = getattr(qa, "id", None) or qa.model_dump().get("id")

        print("creating agent claims-triage...")
        # reflection=True is the platform's native version of P3's self-critique
        # step. See PLATFORM_NOTES.md for what it does and does not give you.
        triage = await studio.acreate_agent(
            name="claims-triage",
            provider="openai",
            role="First-line claims triage assistant",
            goal="Identify required evidence and recommend settle, refer or deny",
            instructions=TRIAGE_INSTRUCTIONS,
            temperature=0.0,
            reflection=True,
            store_messages=True)
        ids["claims-triage"] = getattr(triage, "id", None) or triage.model_dump().get("id")

        json.dump(ids, open(OUT, "w"), indent=2)
        print(f"\nwrote {OUT}")
        print(json.dumps(ids, indent=2))
        print("\nVerify with ONE run:")
        print("  PROVIDER=lyzr RETRIEVER=lyzr python3 p02_rag_citations/main.py --demo")
        print("\nFREE TIER: 500 credits/month, tokens billed separately.")
        print("Do NOT run --all or golden-set sweeps against PROVIDER=lyzr.")

    finally:
        await studio.aclose()


if __name__ == "__main__":
    # Studio exposes async methods only (acreate_agent, aget_agent, ...) plus
    # close(). Verified against lyzr-adk 0.1.12 by inspecting the client.
    asyncio.run(main())
