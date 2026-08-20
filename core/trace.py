"""
Span tracing. Every LLM call and tool call in every project emits one span.

Why it is in core rather than in P11: instrumentation cannot be retrofitted.
If P1..P8 don't emit spans while they run, P11 has nothing to show. P11 is
therefore a *view* over data the other projects have been writing all along --
which is exactly how it works at a real company.
"""
from __future__ import annotations
import time, json, contextvars
from contextlib import contextmanager
from typing import Optional
from .store import db, new_id
from .llm import get_provider, LLMResponse

_run_id = contextvars.ContextVar("run_id", default=None)
_parent = contextvars.ContextVar("parent_span", default=None)


@contextmanager
def run(project: str, claim_id: str = None, meta: dict = None):
    rid = new_id("run")
    with db() as c:
        c.execute("INSERT INTO runs(run_id,project,claim_id,status,started_at,meta)"
                  " VALUES(?,?,?,?,?,?)",
                  (rid, project, claim_id, "running", time.time(),
                   json.dumps(meta or {})))
    tok = _run_id.set(rid)
    status = "ok"
    try:
        yield rid
    except Exception as e:
        status = "failed"
        raise
    finally:
        with db() as c:
            cost = c.execute("SELECT COALESCE(SUM(cost_usd),0) s FROM spans"
                             " WHERE run_id=?", (rid,)).fetchone()["s"]
            cur = c.execute("SELECT status FROM runs WHERE run_id=?",
                            (rid,)).fetchone()["status"]
            # a run paused by P6 stays paused; do not overwrite it
            final = cur if cur == "paused" else status
            c.execute("UPDATE runs SET status=?,ended_at=?,cost_usd=? WHERE run_id=?",
                      (final, time.time(), cost, rid))
        _run_id.reset(tok)


@contextmanager
def span(name: str, kind: str = "logic", **attrs):
    sid, t0 = new_id("sp"), time.time()
    ptok = _parent.set(sid)
    rec = {"ok": 1, "model": None, "tin": 0, "tout": 0, "cost": 0.0, "attrs": attrs}
    try:
        yield rec
    except Exception as e:
        rec["ok"] = 0
        rec["attrs"]["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        _parent.reset(ptok)
        rid = _run_id.get()
        if rid is None:
            # a span emitted outside run() -- a bug, but losing telemetry is
            # worse than recording it under an orphan bucket. Surfaced in the
            # dashboard as project='__orphan__' so it gets noticed.
            rid = "run_orphan"
            with db() as c:
                c.execute("INSERT OR IGNORE INTO runs(run_id,project,status,"
                          "started_at) VALUES(?,?,?,?)",
                          (rid, "__orphan__", "ok", t0))
        with db() as c:
            c.execute(
                "INSERT INTO spans(span_id,run_id,parent_id,name,kind,started_at,"
                "duration_ms,ok,model,tokens_in,tokens_out,cost_usd,attrs)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, rid, _parent.get(), name, kind, t0,
                 int((time.time() - t0) * 1000), rec["ok"], rec["model"],
                 rec["tin"], rec["tout"], rec["cost"],
                 json.dumps(rec["attrs"], default=str)))


def call_llm(prompt: str, model: str, system: str = "",
             span_name: str = "llm", **kw) -> LLMResponse:
    """The only way any project should talk to a model."""
    with span(span_name, kind="llm", model=model,
              attempt=kw.get("attempt", 0)) as s:
        r = get_provider().complete(prompt, model, system=system, **kw)
        s.update(model=r.model, tin=r.tokens_in, tout=r.tokens_out, cost=r.cost_usd)
        s["attrs"]["backend"] = r.backend
        s["attrs"]["chars_out"] = len(r.text)
        return r
