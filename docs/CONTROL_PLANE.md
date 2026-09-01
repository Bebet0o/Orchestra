# Plan de contrôle Orchestra

Le plan de contrôle est indépendant des sessions Hermes.

## Registre déclaratif

Les projets actifs sont décrits dans :

`config/projects.d/*.toml`

Les fichiers sont validés avant toute synchronisation dans SQLite.

Contraintes initiales :

- identifiant de projet stable ;
- repository sous `workspaces/` ;
- données sous `project-data/` ;
- aucun push automatique ;
- un seul writer par projet ;
- reviewer obligatoire ;
- politique existante obligatoire.

## État transactionnel

La base se trouve dans :

`state/controller/orchestra.db`

Elle utilise :

- SQLite WAL ;
- clés étrangères actives ;
- attente sur verrou de 5 secondes ;
- synchronisation disque complète ;
- migrations versionnées.

## Entités initiales

- projets ;
- runs ;
- tâches ;
- verrous d'écriture ;
- événements ;
- approbations ;
- résultats de revue ;
- mémoire permanente.

Orchestra ne considère jamais la WebUI, Telegram ou une session LLM comme la
source de vérité transactionnelle. La source de vérité est la base Controller,
complétée par Git et les snapshots.
