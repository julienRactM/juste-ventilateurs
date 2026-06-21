# Reprendre une demo sans perdre les donnees

Ce document explique comment relancer proprement la demo, comment arreter la simulation, et quoi faire avec Grafana quand les anciennes donnees sont encore visibles.

## Regle importante

`docker compose up` ne supprime pas les donnees.

`docker compose down` arrete les conteneurs, mais garde les volumes Docker. Les donnees TimescaleDB et Grafana restent presentes.

`docker compose down -v` supprime les volumes Docker. Cela efface donc :

- les donnees TimescaleDB ;
- les anciennes mesures de simulation ;
- l'etat Grafana ;
- les dashboards crees manuellement dans l'interface Grafana.

Le dashboard versionne du projet reste disponible car il est dans :

```text
grafana/provisioning/dashboards/overheat-agent-dashboard.json
```

## Methode recommandee pour une demo propre

Ne pas supprimer la base avant chaque demo. Il vaut mieux lancer chaque simulation avec une nouvelle date de depart.

Exemple :

```bash
curl -X POST "http://localhost:3333/sim/scenarios/marseille?cadence=1&tickDuration=1h&persist=true&startDate=2028-01-01T00:00:00.000Z"
```

Puis dans Grafana, regler la plage de temps sur :

```text
From: 2028-01-01 00:00:00
To:   2028-02-01 00:00:00
```

Avantage : les anciennes preuves restent sauvegardees, mais la demo est lisible parce que Grafana affiche seulement le nouveau run.

## Arreter la simulation

Pour arreter la simulation en cours :

```bash
curl -X POST http://localhost:3333/sim/stop
```

Verifier que la derniere date sauvegardee ne bouge plus :

```bash
docker compose -f docker-compose.yaml exec -T timescaledb \
  psql -U tsuser -d tsdb \
  -c "SELECT MAX(time) AS last_saved_point FROM sensor_data;"
```

Si besoin, arreter aussi l'agent ML :

```bash
docker compose -f docker-compose.yaml stop juste-ventilateurs
```

## Reprendre la demo sans effacer les donnees

Demarrer les services :

```bash
docker compose -f docker-compose.yaml up -d --build
```

Verifier que les services tournent :

```bash
docker compose -f docker-compose.yaml ps
```

Verifier que les ventilateurs existent :

```bash
curl -s http://localhost:3333/fans | grep -o '"fan_id":[0-9]*' | wc -l
```

Le resultat attendu est :

```text
40
```

Lancer ou relancer l'agent ML :

```bash
docker compose -f docker-compose.yaml up -d --build juste-ventilateurs
```

Lire les logs agent :

```bash
docker compose -f docker-compose.yaml logs -f juste-ventilateurs
```

Pendant la demo, on doit voir :

```text
[PRED]
[ACTION]
```

Selon le cas, on peut aussi voir :

```text
[ACTION-SKIP]
[MAINTENANCE]
```

## Voir les actions dans Grafana

Ouvrir Grafana :

```text
http://localhost:3000
```

Identifiants :

```text
admin / admin
```

Dashboard :

```text
Dashboards > IoT Data Stream > Juste Ventilateurs - Agent ML
```

Le panneau `Actions refroidissement ML` compte les moments ou les ventilateurs sont passes au-dessus de la vitesse de base `35%`.

Ce compteur sert de preuve visuelle que l'agent a agi suite aux predictions du modele. Les logs agent montrent la decision exacte avec `[PRED]` et `[ACTION]`, tandis que Grafana montre l'effet dans les donnees.

Le panneau `Temperature actuelle max` montre la temperature max du dernier tick disponible. Il est plus utile en demo que la temperature max historique, car il evolue pendant la simulation.

## Surveiller les ventilateurs en direct

Dans un terminal separe :

```bash
watch -n 2 'curl -s http://localhost:3333/fans | grep -o "\"speed_percent\":[0-9]*" | sort | uniq -c'
```

Pendant la demo, les vitesses peuvent passer par exemple de :

```text
35
55
75
95
```

Cela montre que l'agent ajuste les ventilateurs dynamiquement.

## Si la base a ete supprimee avec down -v

Si tu as lance :

```bash
docker compose -f docker-compose.yaml down -v
```

alors il faut recreer la topologie avant de lancer l'agent.

Demarrer les services :

```bash
docker compose -f docker-compose.yaml up -d --build
```

Recreer la topologie Marseille :

```bash
curl -X POST http://localhost:3333/build-exercise \
  -H "Content-Type: application/json" \
  -d '{
    "topology": [
      {
        "clusterName": "Marseille",
        "city": "Marseille",
        "configProfile": "MEDIUM_GPU",
        "clusterCount": 1
      }
    ]
  }'
```

Verifier les ventilateurs :

```bash
curl -s http://localhost:3333/fans | grep -o '"fan_id":[0-9]*' | wc -l
```

Puis relancer l'agent :

```bash
docker compose -f docker-compose.yaml up -d --build juste-ventilateurs
```

## Reset complet volontaire

A utiliser seulement si tu veux repartir de zero et que les preuves sont deja sauvegardees.

```bash
docker compose -f docker-compose.yaml down -v
docker compose -f docker-compose.yaml up -d --build
```

Ensuite, refaire la topologie avec `/build-exercise`, puis relancer l'agent.

Pour l'oral, eviter le reset complet juste avant la presentation. Preferer une nouvelle `startDate` et un filtre Grafana propre.
