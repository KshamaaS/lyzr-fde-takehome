# Platform notes — building on Lyzr

Written as an FDE would write it for the product team: where the platform
carried the build, where I had to drop below it, and what would close the gap.

## Where Lyzr is the backend

| Project | Lyzr surface | Why the platform is the right choice |
|---|---|---|
| **P2** RAG | Knowledge Base | Chunking, embedding, vector store are managed. Hand-rolling FAISS here would be slower to build and worse to operate. `LyzrRetriever` in `p02_rag_citations/main.py`. |
| **P4** tools | `@tool` registration | Native tool binding for the lookup tools. |
| Chat surfaces | Agent Studio | Two agents published for the conversational patterns; the control plane is not a chat surface and correctly isn't one. |
| All | ADK provider | `LyzrProvider` in `core/llm.py` — one seam, `PROVIDER=lyzr`. |

## Where I dropped below the platform, and why

Four patterns need code to sit **between the model call and the response**:

| Project | The moment that needs control |
|---|---|
| **P1** | Schema validation fails → build a repair prompt carrying the validator's exact error → retry. The decision point is *after* generation but *before* the caller sees anything. |
| **P3** | Reflect mid-loop and veto a premature conclusion; enforce an iteration cap and a no-progress detector in code. |
| **P7** | Classify with a cheap model, then decide whether to spend on a strong one — a routing decision *inside* one logical task. |
| **P6** | Halt execution and persist state *before* a mutating tool fires, then resume with re-validation. |

`agent.run()` returns a completed response. By the time you hold it, all four
decision points have passed. This is not a criticism of the abstraction — it is
the correct abstraction for the common case — but it is a real boundary, and a
customer's engineer will hit it in about week two.

**What would close it:** a callback or middleware interface on the ADK — hooks
like `on_before_model_call`, `on_after_model_call`, `on_tool_call` that can
inspect, mutate, retry, or halt. That single addition would move P1, P3 and P7
onto the platform natively.

**A smaller one:** the ADK response has no usage block, so `LyzrProvider`
estimates token counts from string length (`core/llm.py`, flagged in `raw`).
Any customer doing cost attribution needs real usage, and P7's entire analysis
would be unreliable on the Lyzr path today.

**A third:** with a managed Knowledge Base, chunk boundaries are not mine to
choose. P2 chunks on policy section headers because a clause is a semantic and a
legal unit — a citation pointing at a fragment split mid-clause is worse than
useless to an adjuster defending a decision. Configurable boundary rules, or
metadata passthrough guaranteeing a stable `section_id`, would matter for any
regulated-document use case.

## Free tier limits, and what they mean for a customer

The Community tier is 500 credits/month, 5 knowledge bases, 100 MB RAG storage,
7-day logs. LLM tokens bill separately at pass-through rates — two meters, not
one. Responsible AI guardrails are excluded below Enterprise, so the PII layer
here is my own code, not the platform's.

Practical consequence: 50 claims × 8 projects × debugging iterations exhausts
500 credits in about a day. So `core/llm.py` takes a config flag — develop and
evaluate on the direct path, run the Lyzr path deliberately for demos and
comparison.

**This is not a workaround; it is the deployment pattern.** A customer under a
credit budget or mid-procurement needs a path that degrades gracefully when
platform quota runs out, and an FDE should hand them the adapter on day one
rather than after the first overage invoice.

**Rough guidance I would give a customer:** below ~500 agent runs/month,
Community is genuinely enough to evaluate. Above that, the credit meter, not the
feature list, is what forces the tier decision — and log retention (7 days) will
force it sooner than credits for anyone with a compliance requirement.
