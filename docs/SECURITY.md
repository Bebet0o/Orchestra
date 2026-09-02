# Orchestra security

## Execution plane

Hermes Agent never accesses the host Docker daemon socket.

Dynamic execution uses a dedicated daemon owned by `orchestra-runtime`:

- privileged service: `orchestra-runtime`
- socket : `/run/orchestra-docker/docker.sock`
- transport TCP : désactivé
- port publié sur l'hôte : aucun
- réseau partagé entre Agent et moteur : aucun
- daemon state: the private `orchestra-runtime-data` volume

The socket volume is shared with the unprivileged application appliance only
for Supervisor, Orchestrator, worker, and reviewer operations. It controls the
private daemon, never the host daemon. Controller remains loopback-only inside
the application container.

## Sandboxes

- backend Hermes : `docker`
- worker publié : `ghcr.io/bebet0o/orchestra-worker@sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49`
- capacités supprimées par défaut ;
- `no-new-privileges` activé ;
- montage automatique du répertoire courant désactivé ;
- secrets non transmis par défaut ;
- persistance validée ;
- daemon hôte et daemon sandbox séparés.

## Console foundation security boundary

The milestone 2P Console is a static, loopback-only, unprivileged service. It
uses an exact route and asset allowlist, a no-network Content Security Policy,
bounded request concurrency, no browser storage, no external dependency, no
API credential, and no direct access to Controller state. Hermes
WebUI and Hermes Agent are not trusted as Console data sources.
