# Rôles Orchestra

## Profils

- `ops-orchestrator`
- `ops-worker-code`
- `ops-worker-tests`
- `ops-worker-docs`
- `ops-reviewer`
- `ops-recovery`

## Authentification

Les profils utilisent tous un lien symbolique vers l'unique magasin OAuth
racine. Cela évite de dupliquer des refresh tokens Codex pouvant diverger.

Aucun jeton n'est présent dans le dépôt Git ou dans les `.env` des profils.

## Frontières

Les SOUL et toolsets décrivent le rôle du modèle, mais ne constituent pas une
frontière de sécurité.

Les règles suivantes seront imposées par le Controller :

- worktree distinct pour chaque transaction ;
- worker monté en écriture uniquement dans son worktree ;
- reviewer monté en lecture seule ;
- orchestrateur sans workspace projet ;
- recovery limité à l'état Controller et aux snapshots ;
- aucun push ;
- une seule transaction d'écriture par projet.

## Source de vérité

Le Kanban Hermes natif n'est pas encore utilisé par les profils Orchestra.

La source de vérité transactionnelle reste :

`state/controller/orchestra.db`

Un plugin Orchestra exposera cette base aux profils dans un prochain jalon.
