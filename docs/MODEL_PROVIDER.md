# Model provider boundary

Milestone 2Y introduced a synchronous, runtime-neutral boundary. Milestone 2Z
now composes it with the first native agent runtime:

```text
NativeRuntime -> ModelProvider -> concrete model backend
```

`NativeRuntime` receives one provider and one fixed model ID, maps one runtime
prompt to one user message, and performs one synchronous generation. The
control-plane factory now selects it explicitly for roles configured with
`runtime.kind = "native"`; the runtime itself still does not construct
providers or route by role. No router is implemented.

## Public contract

`ModelProvider.generate(ModelRequest) -> ModelResult` represents one complete
generation attempt. It is synchronous and non-streaming. A request contains
only an opaque model identifier, a non-empty tuple of typed system/user/
assistant messages, and an explicit finite timeout between zero and 600
seconds (strictly greater than zero). Message content remains arbitrary valid
Unicode data and is excluded
from representations. The package does not interpret model families,
quantization, roles, tasks, sandboxes, Git state, or lifecycle state.

`ModelResult` is success-only and contains one textual output. Empty text is a
valid provider response. `NativeRuntime` transports that text to
`RuntimeResult`; neither boundary decides whether it satisfies an agent
protocol. Business validation remains in the higher-level control-plane and
domain components. Failures raise `ModelProviderError` with one of five
backend-neutral kinds: `unavailable`, `timeout`,
`request_rejected`, `invalid_response`, or `provider_failed`.

`FakeModelProvider` consumes deterministic typed outcomes and records typed
requests. It performs no HTTP, runtime, Docker, or provider-specific work.

## Minimal OpenAI-compatible adapter

`OpenAICompatibleProvider` implements only non-streaming chat completions. It
serializes exactly `model` and `messages`, and accepts a successful response
only when the first choice contains a message with string `content`. Unknown
response fields are ignored. Invalid JSON, invalid UTF-8, missing or malformed
structure, oversized responses, and silent type coercion fail closed.

The exact generation endpoint is injected through
`OpenAICompatibleConfig`; it is not part of `ModelRequest`. Only `http` and
`https` URLs with a host and path are accepted. Userinfo, query strings,
fragments, control characters, and non-ASCII endpoint syntax are rejected.
An API key is optional, private, excluded from representations, and accepted
only with HTTPS. It is sent only as `Authorization: Bearer ...`. There is no
environment-variable or credential-file discovery inside the adapter. The
control-plane runtime factory supplies `ORCHESTRA_NATIVE_ENDPOINT_URL` and, if
present, `ORCHESTRA_NATIVE_API_KEY` as explicit adapter configuration.

The adapter uses Python's standard urllib HTTP and TLS stack. Its concrete
responses are normalized at the transport boundary into a private frozen
structure containing only a strict integer status, bounded bytes, and the
optional validated Content-Length. Business parsing does not depend on the
concrete `HTTPMessage` header representation. HTTPS uses the system
trust configuration; there is no insecure TLS option. Environment proxies are
disabled with an empty `ProxyHandler`, and redirects are refused so an
authorization header cannot be forwarded elsewhere. One `generate` call
makes one logical HTTP attempt: there are no retries or fallbacks.

Requests and responses are each bounded to 8 MiB by default. The response
limit is also a hard adapter ceiling: `max_response_bytes` may select a
smaller positive limit but cannot raise that ceiling. Request JSON is
rejected through a non-allocating lower-bound check before JSON serialization,
then checked exactly after UTF-8 encoding. Responses are checked against
`Content-Length` when present, then read with a `limit + 1` bound and decoded
as strict UTF-8. Limits are explicit adapter configuration and must be positive
integers.

## Error normalization and data safety

The final mapping is:

| Condition | Error kind |
|---|---|
| timeout, including a timeout wrapped by `URLError` | `timeout` |
| DNS, connection refusal, disconnect, or other OS transport failure | `unavailable` |
| HTTP 4xx | `request_rejected` |
| HTTP 5xx | `provider_failed` |
| HTTP 3xx | `provider_failed` with redirect refused |
| invalid status, body size, UTF-8, JSON, or response structure | `invalid_response` |
| unexpected transport adapter failure | `provider_failed` |

Messages created for transport, HTTP, and parsing failures are stable and
controlled. They never incorporate the secondary exception type or message,
endpoint, headers, API key, request body, message content, or raw response
body. A normalized `ModelProviderError` is raised only after leaving the
secondary exception handler, so it retains no secondary exception through
`__cause__` or `__context__` and copies none of its data. HTTP status values
are exact integers and are never coerced. `HTTPError` resources are closed
without reading their bodies.
`KeyboardInterrupt` and `SystemExit` are not normalized.

## Explicit non-responsibilities

The provider does not implement agent loops, tools, function calling,
streaming, SSE, multimodal data, model discovery, selection, routing,
fallback, retry, load balancing, lifecycle policy, Recovery, Git, sandboxing,
or review policy. Those concerns remain outside this milestone.
