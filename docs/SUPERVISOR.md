# Orchestra Supervisor and Watchdog

The Supervisor makes the deterministic Recovery Manager autonomous.

## Execution model

The `supervisor` service is a persistent member of the canonical Orchestra
Compose application. It runs with the selected operator's numeric UID/GID and
depends on the healthy private sandbox engine, Hermes Agent, and Controller.
Compose restarts it after a process crash.

The service:

1. acquires an exclusive non-blocking file lock;
2. registers its process instance in SQLite;
3. waits for the private Docker daemon, `orchestra-sandbox-engine`, the Hermes
   Agent endpoint, and the Controller to become healthy;
4. performs an immediate startup sweep;
5. performs periodic sweeps using the configured interval;
6. records health, decisions, recovery counts, orphan cleanup and errors;
7. restarts automatically after a process crash.

## Fail-closed behavior

No recovery sweep runs while a required core service is unhealthy. The
Supervisor records a `SKIPPED` sweep and retries later. Recovery decisions
remain exclusively `RESUME_SAFE`, `ROLLBACK_SAFE`, and `BLOCK_HUMAN`, as
implemented by the deterministic Recovery Manager.

## Concurrency

`runtime/supervisor/supervisor.lock` is held for the whole daemon lifetime.
A second daemon or manual sweep exits with code 75 and performs no action.

## Configuration

`config/supervisor.toml` defines:

- periodic sweep interval;
- stale heartbeat threshold;
- startup health wait;
- health retry interval;
- Recovery Manager command timeout.

## Operations

```bash
/opt/orchestra/repo/scripts/orchestra-compose.sh ps supervisor
/opt/orchestra/repo/scripts/orchestra-supervisor.py status
/opt/orchestra/repo/scripts/orchestra-compose.sh logs supervisor
```


## Crash restart readiness

A replacement process can start before Python completes its SQLite
registration. Restart validation therefore waits for both durable states: the
old PID is `ABANDONED`, and the replacement PID is `RUNNING`.
