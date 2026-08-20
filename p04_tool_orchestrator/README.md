# P4 — Multi-Tool Orchestrator Agent

**Brief:** Dynamic tool registry, capability-based routing, permission scoping, parallel execution, conflict resolution. *Shows: coordinate complex workflows.*

## Run it

```bash
python3 p04_tool_orchestrator/main.py --demo
python3 -m pytest p04_tool_orchestrator/test_p04.py -v   # 21 tests
```

## Five decisions worth defending

**1. Tools declare capabilities, not names.** The plan says "I need `coverage`", the registry resolves that to a concrete tool. Swapping a fraud vendor is a registry entry, not a code change — which is the thing a customer actually asks for in week three.

**2. Permission is checked by the registry, before execution.** A prompt saying "only adjusters may issue payment" is a suggestion. `registry.call()` raises `PermissionDenied` *before* the function runs. `test_permission_is_checked_before_execution` pins that it is a gate, not an audit of something that already happened.

**3. Tool returns are validated on the boundary.** A tool returning garbage is indistinguishable from a tool returning a payout instruction unless something types the boundary. Return models are declared in the registry — this is the "tool response validation" bullet from P1, applied where tools actually live.

**4. Parallel fan-out with `return_exceptions=True`.** Independent lookups have no reason to be sequential — measured 2.9× on four tools. One failing tool must not cancel the others; the orchestrator needs partial results to decide whether it can still proceed.

**5. Conflict resolution is a deterministic precedence table, not a model call.**

| Rule | Fires when | Decision |
|---|---|---|
| `fraud_deny_overrides_coverage` | fraud says deny | **refer** |
| `fraud_review_with_repeat_history` | fraud says review + ≥2 claims at address | **refer** |
| `coverage_ambiguous` | coverage ambiguous | **refer** |
| `coverage_excluded` | coverage excluded | **deny** |
| `clean_covered` | covered + fraud proceeds | **pay** |
| *(no rule matched)* | anything else | **refer** |

Ordered most-restrictive-first, so an unsafe combination can never fall through to `pay`. **The asymmetry is deliberate:** fraud can veto coverage, coverage can never override a fraud denial. The cost of wrongly referring a good claim is an adjuster's hour; the cost of wrongly paying a fraudulent one is the claim amount plus the precedent.

`test_unmatched_combination_defaults_to_human_not_pay` is the safety property: no unknown state reaches payment.

## Why not let the model resolve conflicts?

Because "the model decides" is not an answer you can give a regulator, and because the resolution is *policy*, not reasoning. A carrier's fraud-versus-coverage precedence is a business decision made by their SIU team. Encoding it as a table means they can read it, change it, and audit it without touching a prompt.

## Known limitations

- **`flaky_vendor_check` fails ~40% by design** to exercise partial-result handling, but there is no circuit breaker: ten claims hitting the same dead vendor each burn their own timeout.
- **The precedence table is static.** A real carrier would want it configurable per claim type and per jurisdiction.
- **No cost-aware tool selection** — when two tools serve one capability, the first registered wins. Reliability and cost are recorded in the registry but not yet used to choose.
