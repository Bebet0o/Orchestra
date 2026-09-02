# TradingBot dans Orchestra

TradingBot est importé comme projet réel sous l'identifiant `tradingbot`.

## Chemins

- dépôt piloté : `/opt/orchestra/workspaces/tradingbot`
- données : `/opt/orchestra/project-data/tradingbot`
- dépôt historique préservé : `/home/trader/projects/TradingBot`
- branche par défaut : `master`

Le travail non commité observé avant l'importation est sauvegardé, reproduit
dans un clone local propre puis enregistré dans un checkpoint Git. Le dépôt
historique reste inchangé.

## Politique

- push interdit par la politique Orchestra et par `remote.origin.pushurl`;
- un seul writer simultané ;
- revue indépendante obligatoire ;
- snapshots et arbre propre obligatoires ;
- aucune transmission de secrets aux agents ;
- paper trading et observation-only préservés.

## Validation

L'importation exige :

```bash
python -m pytest -q -m "not slow and not real_data"
ruff check src tests
```

Une transaction Orchestra réelle est ensuite ouverte puis annulée proprement
afin de vérifier le snapshot, le clone autonome, le verrou projet et le
nettoyage.
