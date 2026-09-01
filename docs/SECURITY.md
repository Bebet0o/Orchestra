# Sécurité Orchestra

## Plan d'exécution

Hermes Agent n'accède jamais au socket Docker du daemon hôte.

Les outils Hermes utilisent un daemon Docker dédié :

- conteneur : `orchestra-sandbox-engine`
- socket : `/run/orchestra-docker/docker.sock`
- transport TCP : désactivé
- port publié sur l'hôte : aucun
- réseau partagé entre Agent et moteur : aucun
- état moteur : `/opt/orchestra/state/sandbox-engine`

Le socket dédié est partagé uniquement avec Hermes Agent et les services
Supervisor/Orchestrator qui doivent gérer les runtimes. Controller, Console et
Notifier ne le reçoivent pas. Il donne le contrôle du moteur sandbox, mais pas
celui du daemon Docker hôte.

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
