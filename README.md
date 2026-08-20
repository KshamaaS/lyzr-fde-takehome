# Lyzr FDE Take-Home — Agentic Claims Operations

Eight agentic patterns implemented as one system: an insurance claims operations
pipeline. One shared runtime, one 50-claim labelled dataset, one control plane.

**Live control plane:** https://lyzr-fde-takehome.streamlit.app/

**Repo:** https://github.com/KshamaaS/lyzr-fde-takehome

**Scoping note (Part 1):** [`SCOPING_NOTE.pdf`](./SCOPING_NOTE.pdf) — P6, Human-in-the-Loop Approval

---

## Status

| | Project | Status | Evidence |
|---|---|---|---|
| **P1** | Structured Output | ✅ Working | 50/50 parsed, retry sweep table, 14 tests |
| **P2** | RAG + Citation Grounding | ✅ Working | citation contract strips uncited sentences; abstains on 2/6 OOD questions |
| **P3** | ReAct Planning | ✅ Working | explicit state machine; loop guard fires in 3 iterations |
| **P4** | Multi-Tool Orchestrator | ✅ Working | 2.9× parallel speedup, permission denial, precedence table |
| **P6** | Human-in-the-Loop Approval | ✅ Working | 11-step lifecycle demo, double-pay blocked, SLA expiry |
| **P7** | Cost-Aware Router | ✅ Working | measured −14.5% (see finding below), break-even analysis |
| **P8** | Event Automation | ✅ Working | idempotency, DLQ, backoff+jitter, 16 tests |
| **P11** | Observability | ⚠️ Partial | dashboard + alerts + offline canary; **live shadow traffic not built** |
| **P5** | Memory | ⛔ Skipped | see below |
| **P9** | Multi-Agent Debate | ⛔ Skipped | see below |
| **P10** | Self-Reflective Auto-Eval | ⛔ Skipped | see below |
| **P12** | OSS Contribution | ⛔ Skipped | bonus; time went to depth on the eight |

**End-to-end:** `python3 pipeline.py --all` runs all 50 claims through
P8→P1→P7→P3→P4→P2→P6, with P11 observing. Gate accuracy against ground-truth
labels: **50/50** (12/12 auto-approved, 38/38 correctly paused).

### Why P5, P9 and P10 were skipped

These were dropped deliberately, not abandoned. The filter was: *can I defend
every design decision in depth?*

- **P5 (Memory).** Relevance scoring and context-compression thresholds would
  have been arbitrary. "Why top-5 by cosine, why compress at 80% of window" has
  no good answer without measurement I did not have time to do. A memory layer I
  cannot justify is worse than one I did not build.
- **P9 (Debate).** I could not identify a mechanism by which adversarial framing
  improves coverage determinations, as opposed to broadening the space of
  considered arguments — which retrieval (P2) already does more cheaply. It adds
  two LLM calls per decision. I would want an A/B against P2 on the golden set
  before recommending it to a customer.
- **P10 (Self-Reflection).** Salvageable only as a measured experiment. The judge
  shares a model family with the generator, so it is correlated and will
  rubber-stamp its own failure modes. Reporting "reflection improved things" from
  a correlated judge would be a misleading result.

P3 contains a reflection step that vetoes premature conclusions, so the
self-critique idea is represented where it has a concrete job to do.

---

## Architecture

```
                    ┌──────────────────────────────────────────┐
  FNOL webhook ───► │ P8  ingest · idempotent · DLQ · backoff   │
                    └────────────────────┬─────────────────────┘
                                         ▼
                    ┌──────────────────────────────────────────┐
                    │ P1  extract → ClaimRecord (schema+repair) │
                    └────────────────────┬─────────────────────┘
                                         ▼
                    ┌──────────────────────────────────────────┐
                    │ P7  classify cheap → early exit → strong  │
                    └────────────────────┬─────────────────────┘
                                         ▼
                    ┌──────────────────────────────────────────┐
                    │ P3  observe→think→act→reflect (capped)    │
                    │       └── calls ──► P4 tool registry      │
                    └────────────────────┬─────────────────────┘
                                         ▼
                    ┌──────────────────────────────────────────┐
                    │ P4  parallel fan-out · permissions ·      │
                    │     deterministic conflict precedence     │
                    └────────────────────┬─────────────────────┘
                                         ▼
                    ┌──────────────────────────────────────────┐
                    │ P2  ground answer in policy w/ citations  │
                    └────────────────────┬─────────────────────┘
                                         ▼
                    ┌──────────────────────────────────────────┐
                    │ P6  gate → auto-approve │ pause for human │
                    └────────────────────┬─────────────────────┘
                                         ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  core/  llm · store · trace · registry     →   data/agent.db     │
   └─────────────────────────────┬───────────────────────────────────┘
                                 ▼
              P11 observability  ·  Streamlit control plane
```

**The one architectural bet:** a shared `core/` written *before* any project.
Cost accounting and span tracing cannot be retrofitted — if P1–P8 don't emit
priced spans while they run, P7 has nothing to compare and P11 has nothing to
show. See `DECISIONS.md` D2 and D3.

Projects never import each other's agent logic. They share `core/` and they
share the database. That is the only coupling.

---

## Quick start

```bash
pip install -r requirements.txt
python3 data/make_claims.py         # generate the 50-claim golden set
python3 pipeline.py --all           # end-to-end, all 50 claims
streamlit run app/streamlit_app.py  # control plane
pytest                              # test suite
```

> **Hosting note.** The control plane is on Streamlit Community Cloud rather
> than Hugging Face Spaces: HF deprecated the built-in Streamlit SDK, and the
> remaining route — a Docker Space — moved behind a paid plan. Streamlit Cloud
> deploys `app/streamlit_app.py` directly from this repo, so there is no second
> copy of the code to keep in sync. Free-tier apps sleep when idle, so a cold
> first load takes ~30 seconds.

Runs fully offline. `PROVIDER=mock` (default) is a deterministic backend that
replays realistic model failure modes — see `DECISIONS.md` D1. Set
`PROVIDER=anthropic` + `ANTHROPIC_API_KEY`, or `PROVIDER=lyzr` + `LYZR_API_KEY`,
for live models.

---

## Headline findings

**1. Cost routing made things *worse* on this workload — and that's the useful result.**

Routing cost 14.5% more than the all-strong baseline and lost 6 accuracy points,
because 35 of 50 claims escalated and paid for both models. Break-even is around
30% routine traffic; the vendor-claimed 40–60% saving needs 80%+. The honest
answer to a customer asking "will this cut our costs 40–60%" is *what fraction of
your claims are routine?* — answerable from their own data in an afternoon, and
it should precede the build. Full table in `p07_cost_router/README.md`.

**2. Cheapest-first tool routing silently dropped the fraud signal.**

The registry picked a cheaper, flakier vendor over the real fraud tool, and a
fraudulent claim came back `pay`. Fixed by routing on *expected* cost
(`price / reliability`). A missing signal is not a cheaper answer. `DECISIONS.md` D12.

**3. A non-reentrant lock deadlocked the audit path.**

`audit()` opens the database and is legitimately called from inside a `db()`
block. Found on P8's first run. `DECISIONS.md` D10.

---

## Repo map

```
core/          llm.py · store.py · trace.py · registry.py
data/          make_claims.py · claims.json · policy_docs/ · agent.db
p01…p11/       main.py · test_pXX.py · README.md   (identical shape each)
pipeline.py    end-to-end chain
app/           streamlit_app.py (control plane)
DECISIONS.md   every non-obvious choice, what was rejected, why
PLATFORM_NOTES.md   where Lyzr fit, where it didn't, what I'd want exposed
HANDOVER.md    runbook
```

Each project README follows the same structure: what the brief asked for, how it
works, decisions worth defending, and known limitations.
