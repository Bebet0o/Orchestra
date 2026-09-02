# Public installation

Orchestra v0.1.0 targets Debian 12+ and Ubuntu 22.04+ on amd64 with Docker
Engine and Docker Compose. The release remains gated until the official images
and release assets have passed trusted publication and fresh-host smoke tests.

## Runtime contract

The public deployment has exactly two top-level services:

- `orchestra`: the unprivileged application and control plane;
- `orchestra-runtime`: the privileged private nested-container authority.

Controller, Console, Supervisor, Orchestrator, and Notifier are supervised
inside `orchestra`. Hermes Agent, Hermes WebUI, workers, reviewers, and
sandboxes are children of the private daemon in `orchestra-runtime`.

Neither service mounts `/var/run/docker.sock` or `/run/docker.sock` from the
host. They communicate through a dedicated volume containing only the private
socket at `/run/orchestra-docker/docker.sock`.

## Self-hosting

The canonical file is `compose/orchestra.yaml`. It has no `build:` directive,
source bind mount, fixed project name, or `container_name`. It can be copied
into an existing Compose project and supports either the default named data
volume or a bind mount:

```bash
ORCHESTRA_DATA_SOURCE=/mnt/appdata/orchestra docker compose \
  -f orchestra.yaml up -d
```

The stable container data path is `/var/lib/orchestra`. Host directory choice
does not change application behavior. The Console is published on port 8080 by
default and can be changed with `ORCHESTRA_PORT`.
When accessed through another hostname, port, or HTTPS reverse proxy, set
`ORCHESTRA_PUBLIC_ORIGIN` to that exact canonical browser origin.

## Comfort installer

The release installer is standalone:

```bash
curl --fail --location --output install.sh \
  https://github.com/Bebet0o/Orchestra/releases/download/v0.1.0/install.sh
chmod 0755 install.sh
./install.sh
```

It checks or installs Docker and Compose, downloads the same canonical Compose
asset, creates `/opt/orchestra/data`, starts the same two images, and waits for
the application health check. It does not require Git, a source checkout, a
local application build, or host Python.

Until trusted application digests exist, development builds can be supplied
explicitly:

```bash
./install.sh \
  --compose-file ./compose/orchestra.yaml \
  --orchestra-image orchestra:dev \
  --runtime-image orchestra-runtime:dev
```

The `latest` tag is rejected. Final release automation will inject accepted
immutable application and runtime references rather than inventing digests.

## Persistent state and first boot

The appliance initializes secrets, creates a fresh schema-24 database, and
runs forward-only migrations at startup. Existing supported data is migrated
under the same fail-closed rules. Provider authentication is optional at boot;
features requiring Hermes Agent remain unavailable until it is configured.

The accepted worker authority remains:

```text
ghcr.io/bebet0o/orchestra-worker@sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49
```

## Health and logs

The `orchestra-runtime` health check verifies its daemon and required Hermes
children. The `orchestra` health check verifies Controller, Console, all
mandatory supervised processes, and access to the private daemon.

Use normal Compose operations:

```bash
docker compose ps
docker compose logs orchestra
docker compose logs orchestra-runtime
```

## Uninstall

`uninstall.sh` removes the two containers while preserving persistent data,
secrets, projects, workspaces, and backups. Data removal is explicit:

```bash
./uninstall.sh --remove-data --confirm REMOVE_DATA
```
