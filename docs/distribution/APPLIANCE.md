# Orchestra appliance architecture

The v0.1.0 public runtime deliberately uses two Compose services so the
privileged nested-container boundary does not collapse into the control plane.

```text
host Docker
  +-- orchestra (unprivileged)
  |     +-- Controller
  |     +-- Console
  |     +-- Supervisor
  |     +-- Orchestrator
  |     +-- Notifier
  |
  +-- orchestra-runtime (privileged)
        +-- private Docker daemon
              +-- Hermes Agent
              +-- Hermes WebUI
              +-- workers / reviewers / sandboxes
```

Only `orchestra-runtime` is privileged. The host Docker socket is never
mounted. The services share a dedicated private socket volume and the stable
`/var/lib/orchestra` data volume so nested bind mounts resolve inside the
runtime container's namespace.

The application image packages only current Python application code, Console
distribution, configuration, profiles, specifications, and migrations. The
runtime image packages Docker DIND plus its small initialization entrypoint.
Hermes remains an upstream integration and is pulled by immutable digest.

The application entrypoint acts as PID 1. It performs idempotent initialization,
waits for the private daemon, starts the five mandatory application processes
in deterministic order, prefixes their output, forwards termination signals,
reaps children, and fails the appliance if a mandatory child exits.

The public Compose file and installer use the same images, mounts, health
checks, startup dependency, and internal paths. `compose/orchestra.dev.yaml`
adds local build directives only for repository development.
