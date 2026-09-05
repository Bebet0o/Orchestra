# Model Router — v0.2.0 milestone 0.2-G completion report

## Scope

Milestone 0.2-G adds deterministic, durable and explainable model selection above
`AgentRuntime` and `ModelProvider` without changing runtime ownership. NativeRuntime
can receive a different opaque model ID per execution according to an ordered
control-plane policy. HermesRuntime remains profile-managed and never records a
model override that Orchestra cannot actually apply.

The milestone deliberately does **not** add provider discovery, probing, automatic
fallback, cost optimization, load balancing, random selection, retry policy, or an
LLM that chooses another LLM.

## Delivered contract

- immutable `ModelRouteRequest`, `ModelRouteRule`, `ModelRoutingPolicy` and
  `ModelRouteDecision` contracts;
- deterministic first-match routing with explicit configured-model fallback;
- explicit `runtime_managed` provenance for Hermes;
- opaque model IDs compatible with local aliases such as
  `local/Qwen3.8:Q4_K_M`;
- canonical policy JSON and SHA-256 identity;
- strict production TOML policy loading from `[model_router]`;
- schema 31 durable policies and immutable per-execution route decisions;
- SQLite guards that independently enforce request, policy, role, task,
  first-match and fallback semantics;
- atomic route-decision persistence plus execution linkage;
- Planner, Worker and Reviewer production dispatch integration;
- NativeRuntime receives the selected model ID through the existing
  `ModelProvider` abstraction;
- Worker and structured Reviewer routes carry task kind when a durable
  orchestration task is available;
- Recovery corrective attempts reuse Worker routing instead of creating a
  competing recovery-specific model authority;
- historical executions remain valid with no fabricated route history.

## Production configuration

The default remains backward compatible:

```toml
[model_router]
version = 1
```

With no rules, NativeRuntime uses the role's configured model and Hermes remains
profile-managed. A local OpenAI-compatible worker model can be selected with an
ordered rule such as:

```toml
[[model_router.rules]]
id = "local-code"
model = "qwen3.8-flash-next"
role_id = "worker_code"
runtime_kind = "native"
```

The role must itself be configured with `runtime = { kind = "native" }`. Routing
selects a model; it does not silently change the AgentRuntime kind.

## Durable authority

Schema 31 adds immutable routing policy and decision authority and nullable
foreign-key links from Planner, Worker and Reviewer execution rows. A production
execution is reserved first, then its route is resolved, persisted and linked
atomically before runtime construction. If linkage fails, the new routing history
created by that transaction is rolled back.

The database rejects forged decisions, second matching rules when an earlier rule
matches, false configured-model fallbacks, role/runtime mismatches and attempts to
rewrite linked history. Once a decision is linked, the execution ID, role ID and
runtime kind used by that provenance are also immutable.

## Milestone commits before the completion documentation commit

| Commit | Tree | Subject |
| --- | --- | --- |
| `b2d05104ce336f1bf1c5a496bd1b12c20b12f3db` | `5ced7bb056412340c713f4bda479e1b356b5ce11` | `feat: add deterministic model router contract` |
| `643821ae63b42aceefda5d643f954f935c6e67ab` | `5518cd5d5233f369faa87e61f828d434a21efca8` | `feat: persist immutable model route decisions` |
| `d2a5655d9f585eb78056742e35a0108307363d62` | `e4ebd98bde1743d4a56c88321666f2828ea9cee4` | `feat: load model router policy configuration` |
| `47d9700865af66333f03762091150de6597e7e7c` | `bd63cc1b29ffc8998d5a23001ac585891cb49854` | `feat: route production agent model selection` |
| `3cffbe445c315904cbf3808e29e3d4682fc1eb5f` | `ad0bfd8300599a8274d9f85f443d1f5ebf2a987e` | `test: cover model router runtime dispatch` |

The final documentation commit/tree is reported in the completion response; a
commit cannot include its own content-derived identity.

## Validation contract

Focused and regression coverage verifies:

- deterministic first-match and policy hashing;
- configured fallback and Hermes runtime-managed semantics;
- strict policy parsing and unknown-field rejection;
- durable idempotence and immutable provenance;
- atomic execution linkage and rollback on linkage failure;
- Native Planner selected-model propagation into `FakeModelProvider`;
- Worker task-kind routing;
- Reviewer model selection at NativeRuntime construction;
- historical schema preservation and SQLite integrity;
- Reviewer/Judge, Recovery, Shared Context, Task Graph, WorkerPool and
  AgentRuntime regressions.

Final branch validation results:

- complete Python suite: **924 tests PASS**;
- static validation: **PASS**;
- migration sequence 1 through 31: **PASS**;
- SQLite `foreign_key_check`: **PASS** (`[]`);
- SQLite `quick_check`: **PASS** (`ok`);
- SQLite `integrity_check`: **PASS** (`ok`);
- secret-sensitive diff review: **PASS**;
- `git diff --check`: **PASS**.

The complete-suite run found two stale schema-30 expectations in historical tests.
They were updated to schema 31. A final adversarial branch audit then found that an
execution row could still mutate its execution/role/runtime identity after linking
an immutable route decision; schema-31 guards and regression coverage now close that
provenance drift path.

## Intentional deferrals

No provider scoring, availability probing, automatic failover, cost/token budget
router, capability discovery, dynamic load balancing, multi-provider secret
management, or Console UI is introduced. Those capabilities can build on the
immutable routing authority later without changing the 0.2-G provenance model.

## Acceptance markers

```text
MODEL_ROUTER_CONTRACT=PASS
MODEL_ROUTER_DETERMINISTIC_FIRST_MATCH=PASS
MODEL_ROUTER_POLICY_HASHED=YES
MODEL_ROUTER_CONFIG_STRICT=YES
MODEL_ROUTER_SCHEMA_VERSION=31
MODEL_ROUTE_DECISION_DURABLE=YES
MODEL_ROUTE_DECISION_IMMUTABLE=YES
MODEL_ROUTE_EXECUTION_LINK_ATOMIC=YES
MODEL_ROUTE_EXECUTION_IDENTITY_IMMUTABLE=YES
MODEL_ROUTE_FALSE_FALLBACK_REJECTED=YES
MODEL_ROUTE_FORGED_PROVENANCE_REJECTED=YES
NATIVE_PLANNER_MODEL_ROUTING=PASS
NATIVE_WORKER_TASK_KIND_ROUTING=PASS
NATIVE_REVIEWER_MODEL_ROUTING=PASS
HERMES_RUNTIME_MODEL_AUTHORITY_PRESERVED=YES
RECOVERY_REUSES_WORKER_ROUTING=YES
HISTORICAL_EXECUTION_ROUTE_FABRICATION=NO
PROVIDER_DISCOVERY_IMPLEMENTED=NO
AUTOMATIC_MODEL_FAILOVER_IMPLEMENTED=NO
LLM_SELECTED_ROUTING_IMPLEMENTED=NO
HOST_DOCKER_SOCKET_INTRODUCED=NO
CONTROL_PLANE_PRIVILEGE_INCREASE=NO
MULTI_AGENT_CONSOLE_IMPLEMENTED_EARLY=NO
FULL_TEST_SUITE=PASS
FULL_TEST_COUNT=924
STATIC_VALIDATION=PASS
MIGRATIONS_1_TO_31=PASS
SQLITE_FOREIGN_KEY_CHECK=PASS
SQLITE_QUICK_CHECK=PASS
SQLITE_INTEGRITY_CHECK=PASS
DIFF_CHECK=PASS
WORKTREE_CLEAN_BEFORE_PUBLICATION=YES
ORCHESTRA_V020_G_MODEL_ROUTER_READY
```
