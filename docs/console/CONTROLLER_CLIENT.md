# Console Browser Session and Controller Client

Status: **implemented and extended through Orchestra 0.2-H**

## Boundary

The browser communicates only with the Orchestra Console origin on
`http://127.0.0.1:8788`. JavaScript never embeds or calls the Controller port
`8765` directly.

The Console service exposes a deliberately narrow same-origin gateway for:

- `GET /api/v1/auth/session`;
- `POST /api/v1/auth/login`;
- `POST /api/v1/auth/csrf`;
- `POST /api/v1/auth/logout`;
- `GET /api/v1/system/capabilities`.

Milestone 2R additionally exposes six exact query-free GET collections for the
operational dashboard: projects, objectives, reviews, recoveries, plans, and
reviewer assignments. Later slices add the bounded project, Blueprint,
objective, multi-agent plan, and review-detail reads required by the current
Console. The only query-bearing HTTP Console route is the bounded Blueprint
comparison with positive integer `from` and `to` values.

Orchestra 0.2-H also exposes exactly one WebSocket relay:
`/api/v1/events`. It is not part of the normal HTTP allowlist. The relay accepts
only an exact same-origin RFC 6455 upgrade with the browser's HttpOnly session
cookie, translates Origin to the Controller's trusted Console origin, forbids
query strings, extensions and subprotocols, validates the upstream `101`
handshake, and then relays frames without interpreting credentials or event
payloads. All other Controller paths remain unavailable. The gateway is not a
general reverse proxy.

## Origin translation

The browser request must be same-origin with the live Console Host. For POST and PATCH
requests, one exact `Origin` header is mandatory. The Console validates that
origin and then sends the Controller's configured trusted origin
`http://127.0.0.1:8787` upstream. This preserves the existing Controller browser
security contract without enabling CORS in the browser-facing Console.

Cross-origin response headers from the Controller are stripped. The Console
reapplies its own CSP, CORP, COOP, permissions, framing, MIME, cache, and request
ID headers.

## Credential handling

- The operator password exists only in the form field and current request body.
- JavaScript clears the password field after every attempt.
- The HTTP-only `orchestra_session` cookie is never visible to JavaScript.
- The Console forwards `Set-Cookie` unchanged.
- No token, password, CSRF value, command, or pending action is written to
  `localStorage`, `sessionStorage`, IndexedDB, a service worker, or a URL.
- Logout obtains a fresh CSRF token and uses a distinct idempotency key.

## Client behavior

`console/src/controller-client.js` is the only source file allowed to call
`fetch`. It provides a closed method set for session, login, CSRF, logout, and
capability reads. Every request uses:

- same-origin relative URLs;
- `credentials: "same-origin"`;
- `cache: "no-store"`;
- redirect rejection;
- a bounded timeout;
- a cryptographically random idempotency key for POST and PATCH requests.

The application does not treat a login response as sufficient. It re-reads the
authoritative session and capabilities before rendering an authenticated state.

## Degraded mode

When the Controller cannot be reached, the Console keeps local navigation
available but reports a degraded state. It does not invent Controller data,
queue mutations, retry passwords, or preserve destructive intent.

## Deferred work

The current Console still does not add:

- Blueprint build, activation, secret binding, or revision deletion;
- project deletion or repository/default-branch mutation;
- review mutation commands;
- objective start, replan, archive or delete commands;
- offline queues, service workers, or browser persistence;
- arbitrary WebSocket targets or a general-purpose API proxy.

The 0.2-H event cursor exists only for the lifetime of the current page session.
When the Controller reports `replay_unavailable`, the UI clears its volatile
event window, refreshes bounded HTTP snapshot data, and reconnects from the
latest sequence instead of inventing missing history.
