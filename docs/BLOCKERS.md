# Blocages connus

## Approbations WebUI par Runs API

État : non utilisées dans Orchestra.

Le mode WebUI Gateway fonctionne, mais la transmission des approbations par
la Runs API présente encore un défaut amont possible : certaines demandes
arrivent avec un `approval_id` vide et ne peuvent pas être validées depuis le
navigateur.

Conséquences :

- `HERMES_WEBUI_GATEWAY_USE_RUNS_API` n'est pas activé ;
- la WebUI n'est pas encore une surface d'approbation fiable ;
- aucune automatisation Orchestra ne doit dépendre d'un clic WebUI ;
- le Controller disposera de sa propre machine d'état d'approbation ;
- Telegram et l'API Controller seront évalués comme surfaces de décision.

Ce blocage doit être retesté avant la phase quasi autonome.

## Kanban Hermes natif et Controller Orchestra

État : volontairement non reliés.

Les profils Orchestra ne reçoivent pas encore le toolset Kanban natif. Utiliser
simultanément le board natif et la base Controller créerait deux sources de
vérité.

Avant l'automatisation, le Controller devra exposer des outils transactionnels
uniques pour créer, réclamer, heartbeat, terminer, bloquer et revoir les tâches.
