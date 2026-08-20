"""Tests for P2. The citation contract is the product, so that is what is tested."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AGENT_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import core.store as store; store.DB_PATH = os.environ["AGENT_DB"]

from p02_rag_citations.main import (
    load_chunks, LocalRetriever, enforce_citations, ask, install_fixture,
    web_search_fallback, ABSTAIN_BELOW)

CH = load_chunks()
R = LocalRetriever(CH)


def test_corpus_loads():
    assert len(CH) >= 15

def test_chunk_ids_are_clause_addressable():
    """A citation must name a clause an adjuster can look up, not an offset."""
    assert all(":SEC-" in c.id for c in CH)

def test_retrieval_finds_water_clause_for_burst_pipe():
    ids = [c.id for _, c in R.search("water damage from a burst pipe")]
    assert any("WATER" in i for i in ids)

def test_retrieval_finds_theft_notice_clause():
    ids = [c.id for _, c in R.search("theft reported 45 days after loss")]
    assert any("THEFT" in i for i in ids)

def test_off_domain_query_scores_near_zero():
    top = R.search("what is the capital of France")[0][0]
    assert top < ABSTAIN_BELOW

# ---- citation enforcement -------------------------------------------------
ALLOWED = {"HO3_SEC_A_WATER:SEC-1", "HO3_SEC_A_WATER:SEC-2"}

def test_cited_sentence_survives():
    clean, cites, dropped = enforce_citations(
        "Water discharge is covered [HO3_SEC_A_WATER:SEC-1].", ALLOWED)
    assert clean and cites == ["HO3_SEC_A_WATER:SEC-1"] and not dropped

def test_uncited_sentence_is_dropped():
    clean, _, dropped = enforce_citations("Most carriers apply a deductible.", ALLOWED)
    assert clean == "" and len(dropped) == 1

def test_fabricated_citation_is_dropped():
    """The hallucination shape this module exists to catch."""
    _, cites, dropped = enforce_citations(
        "An exclusion applies [HO3_SEC_A_WATER:SEC-99].", ALLOWED)
    assert not cites and "fabricated" in dropped[0]

def test_mixed_answer_keeps_only_grounded_sentences():
    clean, cites, dropped = enforce_citations(
        "Covered under discharge [HO3_SEC_A_WATER:SEC-1]. "
        "Carriers usually also charge a fee. "
        "See also [FAKE_DOC:SEC-3].", ALLOWED)
    assert cites == ["HO3_SEC_A_WATER:SEC-1"] and len(dropped) == 2

# ---- end to end -----------------------------------------------------------
def test_in_domain_question_answers_with_citations():
    install_fixture()
    r = ask("Is flood damage covered?")
    assert r["status"] == "ok" and r["citations"]

def test_every_returned_citation_was_actually_retrieved():
    install_fixture()
    r = ask("Is theft covered if reported 45 days after discovery?")
    if r["status"] == "ok":
        assert set(r["citations"]) <= {s["id"] for s in r["sources"]}

def test_off_domain_question_abstains_before_generating():
    install_fixture()
    r = ask("What is the capital of France?")
    assert r["status"] == "abstained"

def test_abstain_returns_labelled_non_authoritative_fallback():
    fb = web_search_fallback("anything")
    assert fb["authoritative"] is False
