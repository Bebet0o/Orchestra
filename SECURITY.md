# Security policy

Orchestra manages source repositories, persistent orchestration state, model
and runtime boundaries, and containerized worker environments. This policy
explains how to report security problems and the boundaries contributors and
operators should preserve.

## Reporting a vulnerability

Do not open a public issue containing exploit details, secrets, private logs,
or reproduction material for an undisclosed vulnerability.

GitHub private vulnerability reporting is not currently enabled, and the
project does not publish a security email address. If you do not already have a
private maintainer channel, open a public issue with no sensitive detail and ask
the maintainers to arrange one. Do not identify the vulnerable component,
exploit method, affected data, or proof of concept in that request.

Once a private channel is available, a useful report includes:

- the affected commit, component, and deployment assumptions;
- impact and the authority boundary that can be crossed;
- minimal reproduction steps or a proof of concept;
- whether credentials, repositories, worker artifacts, or persisted state may
  have been exposed or modified;
- suggested mitigations, if known.

Do not send live credentials or unnecessarily sensitive production data.

## Scope

Security-relevant surfaces include:

- Controller HTTP/WebSocket authentication, authorization, commands, and
  persistence;
- Console session handling and its allowlisted same-origin Controller gateway;
- worker and reviewer sandbox/environment isolation;
- worker-image build, publication, acceptance, and OCI supply-chain identity;
- `AgentRuntime`, `HermesRuntime`, `NativeRuntime`, and `ModelProvider`
  boundaries;
- Git workspaces, snapshots, review, integration, and recovery;
- secrets, provider authentication, logs, notifications, and backups;
- forward-only database migrations and stored integrity data.

Operational hardening of the host, Docker installation, network, SSH tunnels,
reverse proxies, and provider accounts is also important but may fall outside a
code defect in Orchestra itself.

## Security model and important boundaries

- Published worker authority is an immutable OCI
  `repository@sha256:digest`, verified after an exact pull. Mutable tags and
  names are not authority.
- A local Docker image configuration ID is daemon-local evidence, not an OCI
  digest and not a cross-host artifact identifier.
- Worker and reviewer environments pass through shared resolution,
  materialization, and exact RepoDigest verification.
- The dedicated nested Docker engine isolates sandbox operations. Control-plane
  containers and agents must not receive the host Docker socket.
- Reviewer execution is independent and read-only, without project remotes or
  network access under the current policy.
- State-changing Controller operations use authentication, CSRF protection,
  idempotency, revision checks, audit records, and transactional persistence as
  applicable.
- Malformed or inconsistent persisted state, migration input, runtime output,
  or recovery evidence should fail closed rather than silently acquiring new
  authority.
- Names, labels, paths, and mutable tags alone never prove resource ownership.

These controls reduce risk; they are not a security certification. Operators
remain responsible for access control, backups, credential lifecycle, host and
network hardening, and review of generated changes.

## Secrets

Never commit credentials, `auth.json`, provider tokens, session material,
private environment files, SQLite databases, runtime state, sensitive logs, or
backup archives. The repository includes secret scanning in `validate.sh` and
GitHub secret scanning with push protection, but scanners do not replace review.

Workers and reviewers should receive only the minimum inputs required for their
role. Do not place secrets in Blueprints, prompts, test fixtures, image labels,
publication records, or normalized error messages.

If a credential is exposed, revoke or rotate it through its provider; removing
it from a later commit is not sufficient.

## Supported versions

Orchestra has no formal stable-release support matrix yet. Development occurs
on the default branch, and older commits or historical Orchestra releases do
not receive an implied security-maintenance guarantee.

When reporting a problem, identify the exact commit or deployed artifact. Check
the current default branch for an existing fix, but do not publicly reveal an
unpatched vulnerability while doing so.

## Disclosure

Please allow maintainers a reasonable opportunity to reproduce, assess, and
coordinate a fix before public disclosure. The project does not promise a
specific response or remediation SLA and does not currently advertise a bug
bounty. Credit and disclosure timing should be agreed through the private
channel when possible.
