# Architecture Orchestra

## Plan de contrôle

- Hermes WebUI : interface utilisateur uniquement.
- Hermes Agent : Gateway et API interne.
- Orchestra Controller : machine d'état et attribution des tâches.
- Recovery Manager : reprise, rollback ou blocage humain.
- Supervisor Compose : surveillance et reprise déterministes du plan de contrôle.

## Plan d'exécution

- Orchestrateur par projet.
- Workers spécialisés.
- Reviewer indépendant.
- Frontière [`AgentRuntime`](AGENT_RUNTIME.md) entre le plan de contrôle et
  l'exécution IA ; `HermesRuntime` est l'adapter transitionnel et
  `NativeRuntime` est le premier backend natif.
- `NativeRuntime` dépend de la frontière [`ModelProvider`](MODEL_PROVIDER.md)
  pour une génération synchrone avec un provider et un modèle fixes ; aucun
  router ou choix de modèle par rôle n'est encore implémenté.
- Événements d'exécution typés et liés à la request (`STARTED`, `HEARTBEAT`) ;
  ils transportent des faits runtime, jamais une décision lifecycle ou métier.
- Projection d'erreur commune pour le journal durable, sans déplacer la
  persistance, Git, review ou Recovery dans le runtime.
- Adoption et cleanup des conteneurs fail-closed : labels d'ownership,
  identité cohérente et binding durable sont requis ; un nom ressemblant à
  Orchestra n'est jamais une preuve de propriété.
- Worktrees Git isolés.
- Une transaction d'écriture active par projet.

## Stockage

- `state/hermes-home` : état partagé exigé par Hermes.
- `state/controller` : état transactionnel Orchestra.
- `workspaces` : dépôts et worktrees des projets.
- `project-data` : données non Git propres aux projets.
- `backups` : bundles, patches et snapshots.
- `secrets` : identifiants hors Git.
- `logs` : journaux d'exploitation.
- `runtime` : verrous, PID et état éphémère.
