# Orchestra state after milestone 4C

Orchestra now exposes a persistent operator layer above the autonomous
objective queue. The Notifier watches durable objective events, controller
events and human approvals, creates deduplicated outbox entries and records
every delivery attempt.

The Notifier is a permanent Compose-owned service with an exclusive lock,
heartbeat, crash reconciliation and bounded retry/backoff. File delivery is
always available. Telegram delivery is direct, optional and secret-driven;
credentials are stored outside Git and are never required for controller
correctness.

`orchestractl` provides one consistent interface for objective submission,
queue inspection, pause, resume, cancellation, approval inspection, safe
approval resolution and notification status.

## TradingBot — onboarding 4D-A

Le projet réel `tradingbot` est enregistré sur la branche `master` dans
`/opt/orchestra/workspaces/tradingbot`. Le travail L2 proxy non commité
du dépôt historique a été sauvegardé puis importé dans un checkpoint local
propre. Le push est désactivé, la revue est obligatoire et le dépôt historique
reste intact.

## TradingBot — premier objectif réel 4D-B

L'objectif `objective-3ea998f036434087ba1e5b12d3b3ebcf` a terminé le pipeline AI complet sur le projet
`tradingbot`. Le commit intégré est `59d232008a89a5a671462af7f0805dfa5e50ca55`. Le changement est
strictement documentaire, la revalidation hors ligne et Ruff sont passés, et
Telegram a reçu l'événement `OBJECTIVE_COMPLETED`.
