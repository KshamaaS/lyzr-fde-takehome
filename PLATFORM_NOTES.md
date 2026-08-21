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

---

## Addendum — findings from the actual SDK (lyzr-adk 0.1.12)

The published docs and the shipped SDK differ substantially. Everything below was
established by inspecting the installed package, not from documentation.

### 1. Import name does not match the package name
`pip install lyzr-adk` installs a module imported as `lyzr`. `import lyzr_adk`
fails. Minor, but it is the first thing a customer's engineer hits.

### 2. `Studio` is async-only
Every client method is `a`-prefixed — `acreate_agent`, `aget_agent`,
`acreate_knowledge_base`, `alist_agents`. The only synchronous member is
`close()`. Entities returned by those calls (`Agent`, `KnowledgeBase`) expose
both sync and async methods, so the working pattern is: resolve handles
asynchronously once, then use the entity synchronously.

This is not documented anywhere I could find, and it means a naive
`studio.create_agent(...)` — the shape the docs imply — fails.

### 3. The platform covers more than the docs suggest
`AgentModule.create()` takes as first-class parameters:

| Parameter | Corresponds to |
|---|---|
| `response_model: Type[BaseModel]` | P1 schema enforcement |
| `reflection: bool` | P3 self-critique |
| `llm_judge: bool` | P10 (the project I skipped) |
| `groundedness_facts: List[str]` | P2 citation grounding |
| `bias_check: bool` | — |
| `rai_policy: RAIPolicy` | PII, toxicity, prompt-injection config |
| `memory: MemoryConfig \| CognisConfig` | P5 memory |
| `tools`, `local_tools`, `tool_configs` | P4 tool registry |

`RAIModule` ships with `PIIConfig`, `ToxicityConfig`, `PromptInjectionConfig`
and `SecretsConfig`. `ToolRegistry` and `Tool` exist natively. `SchedulerModule`
covers the trigger half of P8.

**This strengthens rather than weakens the boundary I drew.** These are all
*configuration flags on a single `Agent.run()`*. The gap is not capability, it
is control flow:

- `reflection=True` runs a self-critique — but I cannot read what the reflection
  concluded and branch on it, cap iterations on a no-progress signal, or
  terminate on a repeated action. P3's loop guard is not expressible.
- `response_model` validates — but when validation fails I cannot construct a
  repair prompt carrying the validator's exact error text and retry. That single
  line is what takes P1 from 62% to 100%.
- `llm_judge=True` scores — but I cannot regenerate under constraints derived
  from the critique.
- Nothing halts execution *before* a mutating tool fires, which is the entire
  requirement of P6.

### 4. Cost accounting is not available
`Agent.run()` returns no usage block. `LyzrProvider` estimates tokens from
character counts and flags them as estimates in `LLMResponse.raw`. This is why
every P7 measurement in this repo runs on the direct provider — a cost
comparison built on estimated tokens would not be worth reporting.

**This is the single most valuable thing I would ask the platform team for.**
A customer cannot do cost attribution, chargeback, or budget enforcement without
per-call token counts, and it cannot be reconstructed downstream.

### 5. Default credential routes to OpenAI
`llm_credential_id` defaults to `'lyzr_openai'`. Using Anthropic requires
configuring a credential in Studio first — worth knowing before promising a
customer a specific model.

### What I would ask for, in priority order

1. **Usage/token counts on `AgentResponse`** — blocks all cost work.
2. **A callback or generator seam on the agent loop** so reflection output and
   tool-call intent are observable and interceptable before the next step.
3. **A pre-tool-execution hook** — enough on its own to make P6 expressible natively.
4. **Sync wrappers on `Studio`**, or documentation stating it is async-only.
