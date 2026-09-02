# Transactions Git Orchestra

## Source de vérité

Une transaction est représentée simultanément par :

- un run dans SQLite ;
- un verrou exclusif `project_locks` ;
- un snapshot vérifié ;
- une branche transactionnelle ;
- un worktree Git isolé et verrouillé.

## Début de transaction

Le Controller exige :

- projet activé ;
- repository présent ;
- branche principale attendue ;
- worktree principal propre ;
- aucun writer actif.

Il crée ensuite :

- un bundle Git complet ;
- un patch binaire du worktree ;
- un état porcelain v2 ;
- un inventaire des références ;
- un manifeste et leurs SHA-256 ;
- une branche `orchestra/run/<run-id>` ;
- un worktree sous `.orchestra-worktrees/`.

## Soumission

Une soumission exige :

- worktree propre ;
- au moins un commit supplémentaire ;
- résultat descendant du commit de base ;
- verrou de transaction encore actif.

Le run passe alors de `RUNNING` à `REVIEWING`.

## Rollback

`ROLLBACK_SAFE` exige que le snapshot soit vérifié avant toute suppression.

Le moteur :

- déverrouille et supprime le worktree ;
- supprime la branche transactionnelle ;
- conserve les snapshots et événements ;
- retire le verrou SQLite ;
- place le run en `CANCELLED`.

## Limite actuelle

Le jalon 3A ne fusionne aucun résultat.

L'acceptation par le reviewer, la fusion contrôlée et les montages worker/reviewer
seront ajoutés dans les jalons suivants.
