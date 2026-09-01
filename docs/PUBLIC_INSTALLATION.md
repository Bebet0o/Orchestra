# Installation publique

## Contrat de plateforme

Le chemin public actuel prend en charge :

- Debian 12 ou version ultérieure sur amd64 ;
- Ubuntu 22.04 ou version ultérieure sur amd64 ;
- un utilisateur opérateur local avec une identité UID/GID numérique valide ;
- la racine fixe `/opt/orchestra` ;
- Docker Engine et Docker CLI amont `29.6.1` ;
- le plugin Docker Compose amont `5.3.0`.

L'éligibilité du système et la disponibilité des paquets sont deux contrôles
distincts. L'installateur choisit le dépôt APT Docker correspondant au système
validé (`linux/debian` ou `linux/ubuntu`), puis résout exactement une révision
de paquet pour chaque version amont verrouillée. Une version absente ou une
résolution ambiguë bloque l'installation. Une révision Debian n'est jamais
installée sur Ubuntu.

## Préflight et installation

```bash
git clone https://github.com/Bebet0o/Orchestra.git
cd Orchestra

./preflight.sh
./install.sh --user "$USER"
```

`preflight.sh` est en lecture seule. L'installateur dérive l'UID et le GID
effectifs de l'utilisateur choisi ; aucune identité `1000:1000` n'est imposée.
Si l'utilisateur doit être ajouté au groupe `docker`, l'installation s'arrête
avec `RELOGIN_REQUIRED`. Fermez alors la session, reconnectez-vous, puis
relancez la même commande.

Un fichier d'authentification OpenAI Codex existant peut être fourni sans être
affiché :

```bash
./install.sh --user "$USER" --auth-file "$HOME/auth.json"
```

Une installation divergente exige `--upgrade` et crée auparavant un bundle Git
ainsi qu'une sauvegarde SQLite cohérente. `--skip-start` installe et migre les
données sans démarrer la pile.

## Cycle de vie Compose

Docker Compose possède tous les processus de longue durée :

- Controller API ;
- Console ;
- Supervisor ;
- Orchestrator ;
- Notifier ;
- moteur sandbox privé ;
- intégration Hermes Agent et Hermes WebUI.

Il n'existe aucune unité applicative user-systemd, aucune exigence de linger,
de bus DBus utilisateur ou de `systemctl --user`. Les services HTTP restent
liés à la boucle locale : Controller `127.0.0.1:8765`, Hermes Agent
`127.0.0.1:8642`, Hermes WebUI `127.0.0.1:8787` et Console
`127.0.0.1:8788`.

```bash
ORCHESTRA_ROOT=/opt/orchestra \
ORCHESTRA_UID="$(id -u)" ORCHESTRA_GID="$(id -g)" \
  /opt/orchestra/repo/scripts/orchestra-compose.sh ps

curl --fail http://127.0.0.1:8765/health
curl --fail http://127.0.0.1:8788/health
```

## Autorité Docker et worker

Les opérations dynamiques utilisent exclusivement le daemon DIND privé via
`/run/orchestra-docker/docker.sock`. Le socket Docker de l'hôte n'est monté
dans aucun conteneur du plan de contrôle.

Le worker par défaut est l'image OCI immuable publiée :

```text
ghcr.io/bebet0o/orchestra-worker@sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49
```

L'installation effectue un pull exact dans le daemon privé et vérifie le
RepoDigest exact. Il n'existe plus de contrat d'archive worker, de mode hors
ligne partiel, de tag local autoritaire, ni de fallback basé sur l'ID local
d'une image Docker.

## Installation historique

Une racine historique `/opt/docker/hermesops` ou une ancienne unité
applicative est détectée uniquement pour empêcher une installation parallèle.
La détection se produit avant toute mutation et bloque avec une demande de
procédure de migration explicite. L'installateur ne démarre, n'arrête, ne
migre et ne supprime jamais silencieusement cet état historique.

## Registre initial

Une installation neuve ne crée aucun projet métier. Les fixtures conservées
sous `tests/fixtures/projects/` ne sont installées que sur demande explicite :

```bash
ORCHESTRA_ENABLE_TEST_FIXTURES=1 \
  /opt/orchestra/repo/scripts/init-test-fixtures.sh
```

## Désinstallation non destructive

```bash
./uninstall.sh --user "$USER"
```

La commande arrête la pile Compose et conserve par défaut l'état, les secrets,
les workspaces, les données projet et les sauvegardes. La suppression du dépôt
requiert explicitement `--remove-repo --confirm REMOVE_REPO` et crée d'abord
un bundle Git lorsque le dépôt installé en contient un.
