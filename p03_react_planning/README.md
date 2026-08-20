# P3 — ReAct Planning Agent

**Brief:** Observe→think→act→reflect, max iteration limits, self-critique, graceful degradation. *Shows: agents that don't infinite-loop.*

## The problem in one line

A while-loop wrapped around a prompt will run until your budget is gone. The termination guarantees are the product.

## Run it

```bash
python3 p03_react_planning/main.py --demo
python3 p03_react_planning/main.py --loop     # loop guard test
python3 -m pytest p03_react_planning/test_p03.py -v   # 7 tests
```

## It is a state machine, not a while-loop

```
THINK ──► ACT ──► REFLECT ──┬──► THINK   (evidence incomplete)
  │         │               └──► CONCLUDE
  │         └──► tool error ────► REFLECT
  └──► unparseable plan ────────► ESCALATE

hard exits:  iteration cap (6)  ·  repeated action (2×)  ·  unparseable plan
degradation target: ESCALATE — always a human, never a guess
```

`State` is an explicit `Enum`. That is the difference between an agent whose behaviour you can enumerate and one you have to run to find out about.

## Four termination guarantees

**1. Iteration cap of 6.** Hard ceiling. Three tools exist, each may need one retry — six is two full passes, which is generous for the evidence set and cheap to bound.

**2. No-progress detection.** The same action with identical arguments twice in a row terminates the run. A model repeating itself is not thinking; it is stuck. This fires in 3 iterations under `--loop`.

**3. Unparseable plan escalates immediately.** The agent never improvises when it cannot read its own plan.

**4. Reflection can veto a premature conclusion.** The self-critique step checks whether the required evidence is actually present. If the model tries to conclude with facts missing, reflection sends it back to THINK — bounded by the same cap, so a vetoing reflector cannot itself cause a loop.

## Degradation is to a human, never to a guess

Every terminal path that is not `concluded` returns `escalated` with a termination reason. There is no code path that returns a determination the agent was not confident in. That is what makes it safe to put in front of P6.

**Every run reports why it stopped** — including the happy path. A trace that only explains failures is half an audit trail (see `DECISIONS.md` D15).

## Known limitations

- **The plan is model-generated and only structurally validated.** A plausible but wrong tool choice is caught by reflection, not prevented.
- **Reflection uses the same model family as planning**, so it shares failure modes. This is exactly the correlation problem that led me to skip P10; here it is bounded because reflection only ever *adds* iterations, never approves a payout.
- **No parallel action within an iteration** — that is P4's job; P3 calls it.
