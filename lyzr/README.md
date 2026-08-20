# Lyzr platform assets

Two agents and one knowledge base, created by `setup_agents.py`.

## What runs on Lyzr and why

| Asset | Mirrors | Why the platform is right here |
|---|---|---|
| `claims-policy-corpus` (KB) | P2 retrieval | Chunking, embedding and the vector store are managed. Hand-rolling FAISS would be slower to build and worse to operate. |
| `claims-policy-qa` (agent) | P2 | A knowledge-grounded Q&A agent is exactly the shape a hosted agent expresses well. |
| `claims-triage` (agent) | P3 | Single-turn triage recommendation; the hosted form of the pattern. |

## What is deliberately not here

P1, P6, P7 and the iterative half of P3 are **not** hosted agents, because each needs code between the model call and the response:

- **P1** validates, then builds a repair prompt from the validator's error, then retries
- **P3** reflects mid-loop and can veto its own conclusion
- **P7** classifies with a cheap model then decides whether to spend on a strong one
- **P6** halts execution and persists state *before* a mutating tool fires

`agent.run()` returns a completed response — by the time you hold it, every one of those decision points has passed. This is not a criticism of the platform; it is a boundary, and knowing where it sits is the job. See `../PLATFORM_NOTES.md` for what I would want the ADK to expose to close it.

## Setup

```bash
pip install lyzr-adk
export LYZR_API_KEY=...
python3 lyzr/setup_agents.py
PROVIDER=lyzr RETRIEVER=lyzr python3 p02_rag_citations/main.py --demo
```

## Free tier budget

Community: 500 credits/month, 100 MB knowledge base, 7-day logs, RAI guardrails excluded. LLM tokens bill separately at pass-through rates.

A single 50-claim golden-set sweep across eight projects exhausts the month. **Evaluate on `PROVIDER=mock`; use the Lyzr path for demo runs and the platform comparison only.** The provider seam in `core/llm.py` exists precisely so that switching is a config flag — which is the same discipline you would apply at a customer working inside a credit budget.
