# Projets locaux Orchestra

Ce répertoire contient uniquement les projets activés ou conservés sur
l'installation locale.

Les fichiers `*.toml` de ce répertoire sont volontairement ignorés par Git :
ils contiennent des chemins propres à la machine et ne doivent pas être
publiés dans le dépôt générique.

Une installation publique neuve commence avec **zéro projet enregistré**.

Pour ajouter un projet :

```bash
cp ../examples/project.example.toml ./mon-projet.toml
```

Adapter ensuite les chemins, puis exécuter :

```bash
/opt/orchestra/repo/scripts/orchestra-registry.py validate
/opt/orchestra/repo/scripts/orchestra-registry.py sync
```

Les exemples publiables restent dans `config/examples/`.

Les projets `transaction-fixture*` sont exclusivement des fixtures de
fondation. Leurs modèles sont dans `tests/fixtures/projects/` et ils ne sont
créés qu'après invocation explicite de `scripts/init-test-fixtures.sh`.
