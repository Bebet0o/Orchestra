# Installation publique

## Contrat de la première alpha

Le chemin d'installation public actuel prend en charge :

- Debian 12 ou version ultérieure ;
- Ubuntu 22.04 ou version ultérieure ;
- architecture amd64 ;
- utilisateur de service avec UID/GID `1000:1000` ;
- racine fixe `/opt/docker/hermesops` ;
- Docker Engine testé en `29.6.1` ;
- Docker Compose testé en `5.3.0`.

L'installateur sélectionne le dépôt APT officiel Docker correspondant à Debian
ou Ubuntu et installe les versions amont verrouillées lorsqu'aucun Docker n'est
présent. Il refuse de supprimer automatiquement des paquets Docker concurrents.

## Installation recommandée

```bash
git clone git@github.com:Bebet0o/HermesOps.git
cd HermesOps

./preflight.sh
./install.sh
```

Lors de la première exécution, l'utilisateur peut être ajouté au groupe
`docker`. Dans ce cas, le statut devient `RELOGIN_REQUIRED` : fermez entièrement
la session SSH, reconnectez-vous, puis relancez exactement la même commande.
L'installation est idempotente et reprend sans écraser l'état existant.

Un fichier d'authentification OpenAI Codex existant peut être fourni sans
l'afficher :

```bash
./install.sh --auth-file "$HOME/auth.json"
```

## Installation hors ligne ou test avant release

Avant que l'asset de release soit publié, fournissez l'archive worker exportée
depuis l'installation validée :

```bash
./install.sh \
  --offline \
  --auth-file "$HOME/auth.json" \
  --worker-image-archive \
  "$HOME/hermesops-worker-sandbox-0.2.tar.gz"
```

L'image worker est chargée dans le moteur Docker isolé. Son ID exact doit
correspondre à `config/worker-sandbox.lock.toml`, sinon l'installation échoue
fermée.


## Registre initial

Une installation publique neuve ne crée aucun projet métier et n'enregistre
aucune fixture. Après migration, la table `projects` doit contenir zéro ligne.

Les fixtures de fondation sont conservées sous `tests/fixtures/projects/`.
Elles ne sont installées qu'après une action explicite :

```bash
HERMESOPS_ENABLE_TEST_FIXTURES=1   /opt/docker/hermesops/repo/scripts/init-test-fixtures.sh
```

Cette commande est réservée aux tests du moteur et ne fait pas partie du
bootstrap normal.

## Reprise et sauvegardes

Avant une mise à niveau divergente, l'installateur crée :

- un `git bundle` complet du contrôleur ;
- une sauvegarde cohérente de la base SQLite lorsqu'elle existe.

Il préserve les secrets, `auth.json`, les workspaces, les données projet, les
backups et les fichiers locaux `config/projects.d/*.toml`.

## Désinstallation non destructive

```bash
./uninstall.sh
```

Cette commande désactive les services, retire les copies des unités systemd
utilisateur et arrête les conteneurs sans supprimer les volumes, secrets,
bases, projets ou sauvegardes.

## HermesOps Console foundation (2P)

The dedicated HermesOps Console is served by the user service
`hermesops-console.service` on `127.0.0.1:8788`. The legacy Hermes WebUI remains
on `127.0.0.1:8787` during the beta construction milestones.

Check the service with:

```bash
systemctl --user status hermesops-console.service
curl --fail http://127.0.0.1:8788/health
```

The service is loopback-only. Remote browser access still requires an
operator-managed SSH tunnel or TLS reverse proxy. Milestone 2P exposes no
Controller API proxy and performs no browser authentication or mutation.
