# Démonstration Agent ML Ventilateurs

Ce document sert de runbook pour montrer la boucle complète :

```text
Simulation -> MQTT -> Agent ML -> PATCH /fans -> Ventilateurs -> Grafana/API
```

L'objectif de la démo est de prouver que l'agent utilise le modèle entraîné pour
modifier les ventilateurs en temps réel, sans que le scénario ne force lui-même
les vitesses des ventilateurs.

## 1. Préparer l'environnement

Depuis la racine du projet :

```bash
cd ~/projetcs/iot-data-stream
```

Vérifier que l'environnement Python existe :

```bash
.venv/bin/python --version
```

Vérifier que le modèle est présent :

```bash
ls -lh models/overheat_risk_model_h12.joblib
```

Si le modèle n'existe pas, il faut d'abord relancer l'entraînement avant la
démo.

## 2. Lancer le jumeau numérique

Terminal 1 :

```bash
docker compose -f docker-compose.yaml up -d --build
```

Vérifier que les conteneurs sont démarrés :

```bash
docker compose -f docker-compose.yaml ps
```

Vérifier que l'API répond :

```bash
curl -s http://localhost:3333/documentation > /dev/null && echo "API OK"
```

Vérifier que les ventilateurs sont accessibles :

```bash
curl -s http://localhost:3333/fans | head
```

Grafana est disponible ici :

```text
http://localhost:3000
```

Identifiants Grafana configurés dans `docker-compose.yaml` :

```text
login: admin
password: admin
```

## 3. Remettre les ventilateurs à une base stable

Avant la démo, remettre les 40 ventilateurs à 35%.

```bash
for id in $(seq 1 40); do
  curl -s -X PATCH "http://localhost:3333/fans/$id" \
    -H "Content-Type: application/json" \
    -d '{"speed_percent":35,"control_mode":"AUTO","status":"ON"}' > /dev/null
done
```

Vérifier la répartition des vitesses :

```bash
curl -s http://localhost:3333/fans \
  | grep -o '"speed_percent":[0-9]*' \
  | sort \
  | uniq -c
```

Résultat attendu au départ :

```text
40 "speed_percent":35
```

## 4. Lancer l'agent ML

Terminal 2 :

```bash
.venv/bin/python agents/overheat_model_agent.py \
  --model models/overheat_risk_model_h12.joblib
```

Au démarrage, l'agent doit afficher quelque chose comme :

```text
Loaded model: extra_trees
Fan policy thresholds: low=0.25->55%, medium=0.45->75%, high=0.70->95%
Known servers with fans: 10
Dry run: False
Connected to MQTT broker: Success
Subscribed to v1/gateway/telemetry/#
```

Important :

- `Dry run: False` signifie que l'agent modifie vraiment les ventilateurs.
- `[WARMUP]` signifie que l'agent collecte assez d'historique pour calculer les
  features temporelles.
- `[PRED]` signifie que le modèle prédit une probabilité de risque.
- `[ACTION]` signifie que l'agent a envoyé les commandes `PATCH /fans/{id}`.

Pour tester sans modifier les ventilateurs, ajouter `--dry-run` :

```bash
.venv/bin/python agents/overheat_model_agent.py \
  --model models/overheat_risk_model_h12.joblib \
  --dry-run
```

## 5. Observer les ventilateurs en direct

Terminal 3 :

```bash
watch -n 2 'curl -s http://localhost:3333/fans | grep -o "\"speed_percent\":[0-9]*" | sort | uniq -c'
```

Au début, on doit voir surtout :

```text
40 "speed_percent":35
```

Pendant le scénario, les vitesses doivent changer, par exemple :

```text
24 "speed_percent":35
16 "speed_percent":55
```

ou :

```text
32 "speed_percent":35
4 "speed_percent":75
4 "speed_percent":95
```

Ces changements prouvent que l'agent pilote réellement les ventilateurs via
l'API.

## 6. Lancer le scénario de démonstration

Terminal 4 :

```bash
curl -X POST "http://localhost:3333/sim/scenarios/sc_agent_control_test?cadence=1&tickDuration=1h&persist=true&startDate=2026-12-01T00:00:00.000Z"
```

Ce scénario est fait pour la démo de l'agent :

- il augmente progressivement la charge ;
- il ajoute des dérives thermiques ;
- il ne contient pas de commande `SET_FAN_SPEED` ;
- donc les changements de ventilateurs viennent de l'agent, pas du scénario.

## 7. Ce qu'il faut voir dans les logs agent

Exemples de logs attendus :

```text
[PRED] marseille-marseille-zone-01-worker-01 temp=59.91 load=92.0 fan=35.0 proba=0.446 thresholds=(0.25,0.45,0.70) target_fan=55%
[ACTION] marseille-marseille-zone-01-worker-01: set 4/4 fans [9, 10, 11, 12] to 55%
```

```text
[PRED] marseille-marseille-zone-01-worker-02 temp=59.76 load=92.0 fan=35.0 proba=0.466 thresholds=(0.25,0.45,0.70) target_fan=75%
[ACTION] marseille-marseille-zone-01-worker-02: set 4/4 fans [13, 14, 15, 16] to 75%
```

Interprétation :

- `temp` : température CPU actuelle.
- `load` : charge CPU actuelle.
- `fan` : vitesse moyenne actuelle.
- `proba` : probabilité prédite par le modèle.
- `target_fan` : consigne choisie par l'agent.
- `[ACTION]` : commande envoyée à l'API.

## 8. Vérifier les températures et la simulation en base

Température CPU maximale depuis le début de la démo :

```bash
docker compose -f docker-compose.yaml exec -T timescaledb \
  psql -U tsuser -d tsdb \
  -c "SELECT MAX(sd.value) AS max_cpu_temp
      FROM sensor_data sd
      JOIN sensor s ON s.sensor_id = sd.sensor_id
      WHERE s.sensor_type = 'CPU_TEMP'
        AND sd.time >= '2026-12-01T00:00:00Z';"
```

Nombre de points enregistrés :

```bash
docker compose -f docker-compose.yaml exec -T timescaledb \
  psql -U tsuser -d tsdb \
  -c "SELECT COUNT(*) AS rows
      FROM sensor_data
      WHERE time >= '2026-12-01T00:00:00Z';"
```

Répartition des vitesses des ventilateurs enregistrées :

```bash
docker compose -f docker-compose.yaml exec -T timescaledb \
  psql -U tsuser -d tsdb \
  -c "SELECT sd.value AS fan_speed, COUNT(*) AS rows
      FROM sensor_data sd
      JOIN sensor s ON s.sensor_id = sd.sensor_id
      WHERE s.sensor_type LIKE 'FAN_SPEED_%'
        AND sd.time >= '2026-12-01T00:00:00Z'
      GROUP BY sd.value
      ORDER BY sd.value;"
```

## 9. Arrêter la simulation

Quand la démo est terminée :

```bash
curl -X POST http://localhost:3333/sim/stop
```

Remettre les ventilateurs à 35% :

```bash
for id in $(seq 1 40); do
  curl -s -X PATCH "http://localhost:3333/fans/$id" \
    -H "Content-Type: application/json" \
    -d '{"speed_percent":35,"control_mode":"AUTO","status":"ON"}' > /dev/null
done
```

Si besoin, arrêter toute la stack :

```bash
docker compose -f docker-compose.yaml down
```

Attention : cette commande arrête les conteneurs, mais ne supprime pas les
volumes Docker. Les anciennes données TimescaleDB restent donc présentes sauf
suppression explicite des volumes.

## 10. Maintenance automatique

L'agent contient aussi une détection de maintenance.

Principe :

```text
Si l'agent demande 95% de ventilation,
et que la température CPU reste dangereuse,
et que le refroidissement semble insuffisant,
alors il appelle POST /sim/maintenance/repair avec l'id du ventilateur candidat.
```

La maintenance est activée par défaut :

```bash
.venv/bin/python agents/overheat_model_agent.py \
  --model models/overheat_risk_model_h12.joblib
```

Pour voir la configuration au démarrage :

```text
Maintenance: enabled (temp>=78.0C, cooldown=300s)
```

Pour désactiver la maintenance pendant un test :

```bash
.venv/bin/python agents/overheat_model_agent.py \
  --model models/overheat_risk_model_h12.joblib \
  --disable-maintenance
```

Pour rendre la maintenance plus sensible pendant une démo :

```bash
.venv/bin/python agents/overheat_model_agent.py \
  --model models/overheat_risk_model_h12.joblib \
  --maintenance-temp-threshold 74
```

Log attendu quand une réparation est demandée :

```text
[MAINTENANCE] marseille-marseille-zone-01-worker-01: requested repair for fan 9: ...
```

### Test dédié maintenance

Après modification des scénarios, rebuild l'API pour que le nouveau scénario soit
présent dans le conteneur :

```bash
docker compose -f docker-compose.yaml up -d --build
```

Puis lance le test dédié :

```bash
./scripts/run_maintenance_test.sh
```

Ce script lance :

```text
sc_agent_maintenance_test
```

Le scénario provoque :

- une forte charge ;
- une dérive thermique sur `worker-01` ;
- une panne du ventilateur `9` ;
- aucune restauration manuelle du ventilateur.

Résultat attendu :

```text
[ACTION-SKIP] ... skipped manual/broken fans [9]
[MAINTENANCE] ... requested repair for fan 9 (manual/broken fan detected)
```

Vérification manuelle de la route de maintenance :

```bash
curl -X POST http://localhost:3333/sim/maintenance/repair \
  -H "Content-Type: application/json" \
  -d '{"fanId":9}'
```

## 11. Benchmark et Green IT

Lancer le benchmark complet :

```bash
./scripts/run_benchmark_scenarios.sh
```

Le benchmark compare :

```text
fixed_35   : ventilateurs fixes à 35%
fixed_100  : ventilateurs fixes à 100%
agent_ml   : agent ML actif
```

Après le benchmark, calculer la synthèse Green IT/PUE/ROI :

```bash
./scripts/green_it_roi_summary.sh
```

Paramètres par défaut :

```text
PUE = 1.56
Prix énergie = 0.20 EUR/kWh
```

Pour tester d'autres hypothèses :

```bash
PUE=1.45 ENERGY_PRICE_EUR_KWH=0.25 ./scripts/green_it_roi_summary.sh
```

À présenter à l'oral :

- température maximale ;
- points au-dessus de 70°C ;
- vitesse moyenne ventilateurs ;
- énergie IT estimée ;
- énergie bâtiment avec PUE ;
- coût estimé ;
- économie de l'agent par rapport au mode 100%.

## 12. Captures à préparer pour l'oral

Préparer les captures suivantes :

1. Terminal agent avec les lignes `[PRED]` et `[ACTION]`.
2. Terminal `watch` montrant les vitesses qui passent de 35% à 55%, 75% ou 95%.
3. Grafana avec température CPU, charge CPU et vitesse ventilateurs.
4. Commande SQL montrant la température maximale.
5. Tableau benchmark `fixed_35` / `fixed_100` / `agent_ml`.
6. Tableau Green IT / ROI.
7. Slide avec la politique de décision :

```text
proba < 0.25       -> 35%
0.25 <= proba < 0.45 -> 55%
0.45 <= proba < 0.70 -> 75%
proba >= 0.70      -> 95%
```

## 13. Phrase courte pour expliquer la démo

```text
La simulation publie la télémétrie en MQTT. L'agent Python reçoit chaque message,
reconstruit les features temporelles utilisées à l'entraînement, calcule une
probabilité de surchauffe avec le modèle Extra Trees, puis applique une consigne
de ventilation via l'API PATCH /fans. Le scénario de démonstration ne modifie
jamais directement les ventilateurs : les changements observés viennent donc de
l'agent.
```
