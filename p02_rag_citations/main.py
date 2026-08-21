"""
P2 - RAG Agent with Citation Grounding
Retrieve, answer with sources, flag low confidence, fall back to search.

Retrieval backend is pluggable:
  RETRIEVER=local  -> TF-IDF over the policy corpus (default; no keys, no cost)
  RETRIEVER=lyzr   -> Lyzr Knowledge Base (see PLATFORM_NOTES.md)

The interesting engineering is not retrieval, it is the CITATION CONTRACT:
every sentence of the answer must name the clause it came from, and any
sentence that cannot is stripped before the answer is returned. An answer that
cites nothing is a hallucination with good grammar.
"""
from __future__ import annotations
import sys, os, re, json, math, argparse
from collections import Counter
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.trace import run, span, call_llm
from core.store import audit
from core.llm import get_provider

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "data", "policy_docs")

ABSTAIN_BELOW = 0.12       # retrieval score floor, justified in README
TOP_K = 3
MODEL = os.environ.get("P2_MODEL", "mock-strong")


@dataclass
class Chunk:
    id: str            # e.g. "HO3:SEC-4"
    title: str
    text: str


def load_chunks() -> list[Chunk]:
    """
    Chunked on section headers, not on a fixed token window.

    Policy documents already have the right boundaries: a clause is a semantic
    unit and a legal one. Splitting SEC-4 in half at token 500 would produce a
    citation that points at a fragment, which is worse than useless to an
    adjuster who has to defend the decision.
    """
    out = []
    for fn in sorted(os.listdir(CORPUS)):
        if not fn.endswith(".md"):
            continue
        doc = fn.replace(".md", "")
        raw = open(os.path.join(CORPUS, fn)).read()
        for m in re.finditer(r"^## (SEC-\d+) (.+?)\n(.*?)(?=\n## |\Z)",
                             raw, re.S | re.M):
            out.append(Chunk(f"{doc}:{m.group(1)}", m.group(2).strip(),
                             m.group(3).strip()))
    return out


# ------------------------------------------------------------------ retrieval
_STOP = set("the a an of to and or in on for is are was were be been it its "
            "this that with by from as at we you not any".split())


def _stem(w: str) -> str:
    """
    Deliberately crude suffix stripping, not a real stemmer.
    Justification: the corpus is legal text where 'cover'/'covered'/'coverage'
    must match, and a query for 'burst pipes' must reach a clause about
    'bursting of a pipe'. A full Porter stemmer is more correct but adds a
    dependency and, on a 20-chunk corpus, changes no result.
    """
    for suf in ("ings", "ing", "ages", "age", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _toks(s: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z]+", s.lower())
            if w not in _STOP and len(w) > 2]


class LocalRetriever:
    """TF-IDF cosine. Small corpus, no dependencies, fully inspectable."""
    name = "local"

    def __init__(self, chunks):
        self.chunks = chunks
        self.df = Counter()
        self.tf = []
        for c in chunks:
            t = Counter(_toks(c.title + " " + c.text))
            self.tf.append(t)
            for w in t:
                self.df[w] += 1
        self.N = len(chunks)

    def _vec(self, counts):
        return {w: (1 + math.log(n)) * math.log(self.N / (1 + self.df.get(w, 0)))
                for w, n in counts.items()}

    def search(self, q, k=TOP_K):
        qv = self._vec(Counter(_toks(q)))
        qn = math.sqrt(sum(v * v for v in qv.values())) or 1
        scored = []
        for c, t in zip(self.chunks, self.tf):
            dv = self._vec(t)
            dn = math.sqrt(sum(v * v for v in dv.values())) or 1
            dot = sum(qv.get(w, 0) * dv.get(w, 0) for w in qv)
            scored.append((dot / (qn * dn), c))
        scored.sort(key=lambda x: -x[0])
        return [(round(s, 4), c) for s, c in scored[:k]]


class LyzrRetriever:
    """
    Lyzr Knowledge Base path. The KB owns chunking, embedding and the vector
    store; we own the citation contract on top.

    THE COST OF USING IT (see PLATFORM_NOTES.md):
    chunk boundaries are not ours to choose, and per-document annotation is a
    single `source` string -- there is no metadata dict. So the section id that
    P2's citations depend on cannot be attached at ingestion. We recover it by
    matching the returned text back onto the locally parsed corpus. That works
    here because the corpus is small and the section headers are distinctive;
    it would not scale, and it is the concrete reason clause-level citation
    grounding is not fully expressible on a Lyzr KB today.
    """
    name = "lyzr"

    def __init__(self, chunks):
        import asyncio
        from lyzr import Studio

        ids_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "lyzr", "agent_ids.json")
        kb_id = os.environ.get("LYZR_KB_ID")
        if not kb_id and os.path.exists(ids_path):
            kb_id = json.load(open(ids_path)).get("kb_id")
        if not kb_id:
            raise RuntimeError(
                "no kb_id -- run python3 lyzr/setup_agents.py, or set LYZR_KB_ID")

        self.studio = Studio(api_key=os.environ["LYZR_API_KEY"])
        self.kb = asyncio.run(self.studio.aget_knowledge_base(kb_id))
        self.chunks = {c.id: c for c in chunks}
        # first ~60 chars of each chunk body -> chunk id, for recovering the
        # section id the platform cannot carry for us
        self._by_text = {c.text[:60].strip(): c for c in chunks}

    def _resolve(self, text: str, source: str):
        """Map a returned passage back to a clause id."""
        for prefix, ch in self._by_text.items():
            if prefix and prefix in text:
                return ch
        # fall back: source carries the doc id, but not the section
        return Chunk(f"{source or 'LYZR'}:UNRESOLVED", "", text)

    def search(self, q, k=TOP_K):
        # score_threshold=0.0 deliberately: P2 owns the abstain decision, not
        # the platform. Filtering here would hide the score that ABSTAIN_BELOW
        # is measured against.
        res = self.kb.query(query=q, top_k=k, score_threshold=0.0)
        out = []
        for r in res:
            text = getattr(r, "text", None) or getattr(r, "content", "") or ""
            score = float(getattr(r, "score", 0.0) or 0.0)
            source = getattr(r, "source", None) or ""
            out.append((round(score, 4), self._resolve(text, source)))
        return out


def get_retriever(chunks):
    return (LyzrRetriever(chunks) if os.environ.get("RETRIEVER") == "lyzr"
            else LocalRetriever(chunks))


# ------------------------------------------------------------------- answering
SYSTEM = (
    "P2_POLICY_QA. Answer coverage questions using ONLY the numbered policy "
    "excerpts provided. Every sentence MUST end with a citation in square "
    "brackets, e.g. [HO3:SEC-4]. If the excerpts do not answer the question, "
    'reply exactly: INSUFFICIENT. Return ONLY JSON: {"answer":"...",'
    '"citations":["HO3:SEC-4"],"confidence":0..1}.'
)

CITE_RE = re.compile(r"\[([A-Za-z0-9_]+:SEC-\d+)\]")   # _ : doc ids contain underscores


def enforce_citations(answer: str, allowed: set[str]) -> tuple[str, list[str], list[str]]:
    """
    The citation contract, enforced in code after generation.

    Two failure modes are caught here that a prompt alone will not stop:
      - a sentence with no citation           -> dropped
      - a citation to a clause never retrieved -> dropped (fabricated source)
    Returns (clean answer, kept citations, dropped sentences).
    """
    kept, dropped, cites = [], [], []
    for sent in re.split(r"(?<=[.!?])\s+", answer.strip()):
        if not sent:
            continue
        found = CITE_RE.findall(sent)
        if not found:
            dropped.append(f"[no citation] {sent}")
            continue
        bad = [c for c in found if c not in allowed]
        if bad:
            dropped.append(f"[fabricated source {bad}] {sent}")
            continue
        kept.append(sent)
        cites += found
    return " ".join(kept), sorted(set(cites)), dropped


def web_search_fallback(question: str) -> dict:
    """
    Fallback when the policy corpus cannot answer. Stubbed deliberately: what
    matters for the pattern is that the fallback is CLEARLY LABELLED as
    non-authoritative. In a coverage dispute, an answer sourced from the open
    web must never be presentable as if it came from the policy.
    """
    return {"source": "web_search(stub)",
            "authoritative": False,
            "note": "General guidance only. Not a coverage determination. "
                    "Escalate to an adjuster for a binding answer.",
            "question": question}


def ask(question: str, claim_id: str = None) -> dict:
    chunks = load_chunks()
    retr = get_retriever(chunks)

    with run("p02_rag_citations", claim_id=claim_id,
             meta={"retriever": retr.name}) as rid:

        with span("retrieve", kind="tool") as sp:
            hits = retr.search(question)
            top = hits[0][0] if hits else 0.0
            sp["attrs"].update(top_score=top, k=len(hits),
                               ids=[c.id for _, c in hits])

        # ---- abstain gate: BEFORE generation, not after
        if top < ABSTAIN_BELOW:
            audit("p02", "abstained", claim_id=claim_id, run_id=rid,
                  detail={"question": question, "top_score": top})
            return {"status": "abstained", "reason":
                    f"best retrieval score {top:.3f} below floor {ABSTAIN_BELOW}",
                    "top_score": top, "fallback": web_search_fallback(question),
                    "run_id": rid}

        ctx = "\n\n".join(f"[{c.id}] {c.title}\n{c.text}" for _, c in hits)
        allowed = {c.id for _, c in hits}

        r = call_llm(f"POLICY EXCERPTS:\n{ctx}\n\nQUESTION: {question}",
                     MODEL, system=SYSTEM, span_name="answer")

        try:
            payload = json.loads(_json_of(r.text))
        except json.JSONDecodeError:
            return {"status": "error", "reason": "unparseable answer",
                    "run_id": rid}

        if str(payload.get("answer", "")).strip().upper().startswith("INSUFFICIENT"):
            audit("p02", "insufficient_context", claim_id=claim_id, run_id=rid)
            return {"status": "insufficient", "top_score": top,
                    "fallback": web_search_fallback(question), "run_id": rid}

        with span("enforce_citations", kind="logic") as sp:
            clean, cites, dropped = enforce_citations(payload["answer"], allowed)
            sp["attrs"].update(kept=len(cites), dropped=len(dropped))

        if not clean:
            # everything was stripped -> nothing was grounded -> abstain
            audit("p02", "all_sentences_ungrounded", claim_id=claim_id,
                  run_id=rid, detail={"dropped": dropped})
            return {"status": "abstained",
                    "reason": "no sentence survived citation enforcement",
                    "dropped": dropped, "fallback": web_search_fallback(question),
                    "run_id": rid}

        return {"status": "ok", "answer": clean, "citations": cites,
                "dropped_sentences": dropped,
                "retrieval_score": top,
                "model_confidence": payload.get("confidence"),
                "retriever": retr.name,
                "sources": [{"id": c.id, "title": c.title, "score": s}
                            for s, c in hits],
                "run_id": rid}


def _json_of(t):
    t = re.sub(r"^```(?:json)?|```$", "", t.strip(), flags=re.M).strip()
    i = t.find("{")
    if i == -1:
        return t
    d = 0
    for k, ch in enumerate(t[i:], i):
        if ch == "{":
            d += 1
        elif ch == "}":
            d -= 1
            if d == 0:
                return t[i:k + 1]
    return t[i:]


# ---------------------------------------------------------------- fixture
def install_fixture():
    prov = get_provider()
    if prov.name != "mock":
        return

    def gen(prompt, rng, attempt):
        ids = re.findall(r"\[([A-Za-z0-9_]+:SEC-\d+)\]", prompt)
        q = prompt.split("QUESTION:")[-1].strip().lower()
        if not ids:
            return json.dumps({"answer": "INSUFFICIENT", "citations": [],
                               "confidence": 0.1})

        # ~20% of the time, append an uncited sentence -- the exact hallucination
        # shape the citation contract exists to catch.
        body = []
        if "frozen" in q or "pipe" in q or "freez" in q:
            body.append(f"Freezing damage is excluded where the dwelling was "
                        f"vacant and heat was not maintained [{ids[0]}].")
            if len(ids) > 1:
                body.append(f"Where reasonable care was taken to maintain heat, "
                            f"the resulting water discharge is covered [{ids[1]}].")
        elif "theft" in q:
            body.append(f"Theft claims require prompt notice to police and to "
                        f"the insurer [{ids[0]}].")
            body.append(f"Claims reported more than 30 days after discovery may "
                        f"be denied for late notice [{ids[0]}].")
        elif "flood" in q or "surface water" in q:
            body.append(f"Flood and surface water losses are excluded [{ids[0]}].")
        else:
            body.append(f"The excerpts address this under the cited clause "
                        f"[{ids[0]}].")

        if rng.random() < 0.35:
            body.append("Most carriers also apply a separate deductible in these cases.")
        if rng.random() < 0.15:
            body.append("An additional exclusion applies here [HO3_SEC_A_WATER:SEC-99].")

        return json.dumps({"answer": " ".join(body), "citations": ids,
                           "confidence": round(rng.uniform(0.7, 0.95), 2)})

    prov.register("P2_POLICY_QA", gen)


QUESTIONS = [
    "Is water damage from a burst pipe covered if the house was vacant?",
    "Is theft covered if reported 45 days after discovery?",
    "Is flood damage covered?",
    "What are the insured's duties after a loss?",
    "Does the policy cover damage to my neighbour's fence from my dog?",  # OOD
    "What is the capital of France?",                                     # OOD
]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ask")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    install_fixture()

    if a.ask:
        print(json.dumps(ask(a.ask), indent=2))
    elif a.demo:
        for q in QUESTIONS:
            r = ask(q)
            print(f"\n{'='*64}\nQ: {q}")
            print(f"   status: {r['status']}")
            if r["status"] == "ok":
                print(f"   A: {r['answer']}")
                print(f"   cites: {r['citations']}  retrieval={r['retrieval_score']}")
                if r["dropped_sentences"]:
                    print(f"   DROPPED ({len(r['dropped_sentences'])}):")
                    for d in r["dropped_sentences"]:
                        print(f"     - {d}")
            else:
                print(f"   reason: {r.get('reason')}")
                print(f"   fallback: {r['fallback']['note']}")
    else:
        ap.print_help()
