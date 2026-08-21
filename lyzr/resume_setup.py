"""
Resume provisioning against an EXISTING knowledge base.

setup_agents.py creates a KB. If it failed partway through (e.g. on document
ingestion), rerunning it would create a SECOND KB and burn credits. This script
takes the kb_id you already have and finishes the job.

    LYZR_API_KEY=... python3 lyzr/resume_setup.py <kb_id>
"""
import os, sys, json, glob, asyncio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_ids.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from setup_agents import POLICY_QA_INSTRUCTIONS, TRIAGE_INSTRUCTIONS


async def main(kb_id: str):
    from lyzr import Studio
    studio = Studio(api_key=os.environ["LYZR_API_KEY"])
    ids = {"kb_id": kb_id}
    try:
        kb = await studio.aget_knowledge_base(kb_id)
        print(f"resuming on kb {kb_id}")

        existing = set()
        try:
            for d in await kb.alist_documents():
                src = getattr(d, "source", None) or d.get("source")
                if src:
                    existing.add(src)
            print(f"  already ingested: {sorted(existing) or 'none'}")
        except Exception as e:
            print(f"  (could not list documents: {e})")

        for path in sorted(glob.glob(os.path.join(ROOT, "data", "policy_docs", "*.md"))):
            doc_id = os.path.basename(path).replace(".md", "")
            if doc_id in existing:
                print(f"  skip {doc_id} (already present)")
                continue
            print(f"  adding {doc_id}")
            await kb.aadd_text(text=open(path).read(), source=doc_id)

        for name, role, goal, instr, reflect in (
            ("claims_policy_qa", "Claims policy assistant for a P&C carrier",
             "Answer coverage questions with clause-level citations, or abstain",
             POLICY_QA_INSTRUCTIONS, False),
            ("claims_triage", "First-line claims triage assistant",
             "Identify required evidence and recommend settle, refer or deny",
             TRIAGE_INSTRUCTIONS, True),
        ):
            print(f"creating agent {name}...")
            a = await studio.acreate_agent(
                name=name, provider="OpenAI/gpt-4o-mini", role=role, goal=goal,
                instructions=instr, temperature=0.0,
                reflection=reflect, store_messages=True)
            ids[name] = getattr(a, "id", None) or a.model_dump().get("id")

        json.dump(ids, open(OUT, "w"), indent=2)
        print(f"\nwrote {OUT}")
        print(json.dumps(ids, indent=2))
        print("\nVerify with ONE run:")
        print("  PROVIDER=lyzr RETRIEVER=lyzr python3 p02_rag_citations/main.py --demo")
    finally:
        await studio.aclose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python3 lyzr/resume_setup.py <kb_id>")
    asyncio.run(main(sys.argv[1]))
