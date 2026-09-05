# Model Router

Milestone 0.2-G introduces a deterministic control-plane routing authority above
`AgentRuntime` and `ModelProvider`. The router selects an opaque model ID; it
does not invoke a model, inspect provider internals, or decide whether model
output is correct.

```text
control-plane execution
        |
        v
   ModelRouter
        |
        +-- selected opaque model ID
        |
        v
   AgentRuntime
        |
        +-- NativeRuntime -> ModelProvider
        `-- HermesRuntime
```

## Routing contract

A `ModelRouteRequest` contains only bounded scheduling identity needed for
selection: runtime request ID, role ID, runtime role, runtime kind, the role's
explicit configured model ID, and an optional task kind. It contains no prompt,
context snapshot body, credentials, provider endpoint, Git data, or model
output.

A `ModelRoutingPolicy` is an immutable ordered tuple of at most 64 rules. Each
rule selects one opaque model ID and must constrain at least one of role ID,
runtime role, runtime kind, or task kind. For NativeRuntime, first match wins;
if no rule matches, the only fallback is the role's explicit configured model
ID. HermesRuntime currently keeps model authority in its synchronized Hermes
profile, so the router records that configured model as `runtime_managed` and
never claims that a per-execution override was applied. There is no provider
probing, retry, random choice, load balancing, model discovery, or LLM-based
routing.

Every policy has a canonical SHA-256 over its version, ordered rules, selectors,
and selected model IDs. A `ModelRouteDecision` records the selected model,
policy version/hash, matched rule ID, and whether the result came from a rule or
the configured-model default. The same policy and equivalent request fields
therefore produce the same decision.

This first foundation commit defines and tests the routing domain contract.
Durable persistence, production configuration loading, and planner/worker/
reviewer dispatch integration are added by the remaining 0.2-G work before the
milestone is closed.
