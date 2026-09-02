# Orchestra worker OCI distribution

The production worker and reviewer share one immutable environment authority:

```text
ghcr.io/bebet0o/orchestra-worker@sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49
```

`config/environments/default-worker.toml` records that reference, its exact
digest, `linux/amd64`, and accepted publication provenance. A mutable tag and a
Docker daemon-local image configuration ID are never runtime authority.

## Runtime materialization

Production worker and reviewer preparation follows:

```text
EnvironmentSpec
  -> DefaultEnvironmentResolver
  -> ResolvedEnvironment
  -> NestedDaemonSandboxBackend.materialize()
  -> PreparedEnvironment
```

The backend pulls the complete `repository@sha256:digest` reference into
Orchestra's dedicated private DIND daemon. Inspection must contain that exact
RepoDigest. The resulting local configuration ID is retained only as evidence
for that daemon and cannot replace the OCI digest.

The private daemon socket is `/run/orchestra-docker/docker.sock` inside the
control-plane services that require it. No worker, reviewer, Controller,
Console, Notifier, or other control-plane container mounts the host Docker
socket. Worker and reviewer containers never receive any Docker socket.

## Trusted publication lifecycle

The manually dispatched trusted workflows remain:

- `.github/workflows/publish-worker.yml` builds and pushes an authorized exact
  candidate and emits a provisional digest-bound record;
- `.github/workflows/accept-worker-publication.yml` reauthorizes the candidate,
  performs a fresh anonymous exact-digest pull in an isolated DIND daemon,
  validates RepoDigest/source/revision/version/platform binding, and only then
  emits an accepted record.

Those workflows run only by explicit operator dispatch from their trusted
`main` copies. Installation and ordinary Compose lifecycle operations never
dispatch them, build the worker, push an image, or mutate GHCR visibility.

## Retired distribution paths

The current product has no worker archive import, offline image bundle, local
worker tag authority, local-image-ID distribution lock, legacy environment
adapter, or fallback to an unpublished image. Git history preserves those old
implementations; they are not shipped as dormant compatibility code.
