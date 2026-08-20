# P2 — RAG Agent with Citation Grounding

**Brief:** Retrieve, answer with sources, flag low-confidence, fallback to search. *Shows: prevent hallucinations at scale.*

## The problem in one line

An adjuster who denies a claim has to name the clause. An answer that cites nothing is a hallucination with good grammar.

## Run it

```bash
python3 p02_rag_citations/main.py --demo
python3 p02_rag_citations/main.py --ask "Is flood damage covered?"
python3 -m pytest p02_rag_citations/test_p02.py -v      # 13 tests
RETRIEVER=lyzr python3 p02_rag_citations/main.py --demo # Lyzr Knowledge Base
```

## How it works

```
question ──► retrieve top-3 ──► score < 0.12? ──yes──► ABSTAIN + labelled fallback
                                     │no
                                     ▼
                            generate with excerpts
                                     ▼
                        enforce_citations()  ← the actual product
                          ├─ sentence with no citation      → dropped
                          └─ citation to un-retrieved clause → dropped
                                     ▼
                        nothing survived? ──► ABSTAIN
```

## Three decisions worth defending

**1. Chunk on section headers, not a fixed token window.** Policy documents already carry the right boundaries — a clause is both a semantic and a legal unit. Splitting `SEC-2` at token 500 produces a citation pointing at a fragment, which is worse than useless to someone who has to defend the decision in a dispute.

**2. The citation contract is enforced in code, after generation.** A prompt saying "always cite" is a request. `enforce_citations()` is a control: it drops any sentence without a citation, and any sentence citing a clause that was never retrieved. The demo shows a fabricated `SEC-99` being stripped live.

**3. Abstain runs *before* generation, not after.** If the best retrieval score is below the floor, no model call is made at all. Generating first and then deciding the answer is ungrounded costs money and creates a plausible-sounding artefact somebody may copy out of the logs.

## Why the abstain floor is 0.12

Measured across the corpus: in-domain questions score 0.22–0.47; the two off-domain probes score 0.097 and 0.000. The floor sits in the gap. It is deliberately closer to the noise than to the signal — a false abstain costs an adjuster one lookup, a false answer costs a coverage dispute.

## The fallback is labelled non-authoritative

`web_search_fallback()` returns `{"authoritative": false}` and a note that it is not a coverage determination. In a dispute, an answer sourced from the open web must never be presentable as if it came from the policy. The search itself is stubbed; the labelling is the part that matters.

## Known limitations

- **TF-IDF, not embeddings.** Correct for a 20-chunk corpus and fully inspectable, but it is lexical: a question phrased entirely in synonyms will miss. The Lyzr KB path uses real embeddings and is the answer at scale.
- **Crude suffix stemmer**, not Porter. Justified in the source: on this corpus it changes no result, and it avoids a dependency.
- **`model_confidence` is reported but never gates anything** — retrieval score does. Self-reported confidence is uncalibrated; retrieval score is measured.
- **No multi-hop.** A question needing SEC-2 *and* SEC-4 combined gets both in context but no explicit reasoning chain across them.
