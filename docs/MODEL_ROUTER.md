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
policy version/hash, matched rule ID, and whether the result came from a rule,
the configured-model default, or runtime-managed Hermes authority. The same
policy and equivalent request fields therefore produce the same decision.

## Durable authority

Schema 31 stores the exact canonical policy under its SHA-256 and one immutable
route decision per runtime request/execution identity. The decision preserves
the bounded canonical request, request SHA-256, role/runtime/task selectors,
configured model, selected model, policy version/hash, matched rule and route
reason. Database guards reject decisions that do not match their policy rule or
the configured/runtime-managed semantics.

Planner, worker, and reviewer execution tables now have nullable foreign-key
links to the decision authority. Historical rows remain valid with no fabricated
route. Once a route is linked to an execution, the linkage is immutable.
Production configuration loading is strict and the planner, worker, and reviewer
production dispatch paths all resolve and persist a route before runtime execution.

## Production configuration

The canonical policy is loaded from `config/orchestrator.toml` under
`[model_router]`. The default policy has no rules and therefore preserves
existing behavior. Rules are ordered and strict; unknown fields fail closed.
For example, the policy can express a future production dispatch of one native
worker role to a local OpenAI-compatible model alias without involving Hermes:

```toml
[model_router]
version = 1

[[model_router.rules]]
id = "local-code"
model = "qwen3.8-flash-next"
role_id = "worker_code"
runtime_kind = "native"
```

The role must still use `runtime = { kind = "native" }`; the policy selects the
model while AgentRuntime selection remains explicit role configuration. Planner,
worker, and reviewer production dispatch consume this policy directly.

## Runtime dispatch

Production Planner, Worker, and Reviewer executions now reserve their durable
execution identity, resolve the configured routing policy, persist and atomically
link the immutable `ModelRouteDecision`, and only then construct the runtime. For
NativeRuntime, the selected model ID is passed to the ModelProvider. For
HermesRuntime, the decision remains `runtime_managed` and the synchronized profile
continues to own the actual Hermes model. Corrective recovery attempts reuse the
Worker path, so they receive the same deterministic routing authority without a
second recovery-specific model selector. Injected fake runtimes used by tests do
not fabricate production routing history.
