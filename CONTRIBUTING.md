# Contribuer à Orchestra

Avant une proposition :

1. créez une branche dédiée ;
2. n'ajoutez aucun secret ni état runtime ;
3. exécutez `./validate.sh --static` ;
4. exécutez `./scripts/check-secrets.sh` ;
5. documentez les risques de migration et de récupération.

Ne montez jamais `/var/run/docker.sock` dans les agents et ne transformez
jamais une erreur de reviewer en approbation.
