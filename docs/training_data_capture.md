# Capture De Données Pour Le Modèle Overheat

Objectif : générer des exemples positifs pour le label à `70C`.

Le scénario ajouté est :

```text
sc_training_overheat
```

Trois scénarios supplémentaires ont été ajoutés pour éviter d'entraîner sur une
seule courbe déterministe :

```text
sc_training_fan_loss_overheat
sc_training_load_wave_overheat
sc_training_recovery_boundary
```

Ils produisent volontairement plusieurs phases avec charge forte, ventilation
basse, panne ventilateur, dérive thermique locale et récupération.

## 1. Rebuild Du Simulateur

Depuis la racine du projet :

```bash
docker compose -f docker-compose.yaml up -d --build
```

Attends que l'API soit prête :

```bash
curl http://localhost:3333/documentation
```

## 2. Lancer Le Scénario Chaud

On utilise une date isolée pour exporter uniquement ce run ensuite.

```bash
curl -X POST "http://localhost:3333/sim/scenarios/sc_training_overheat?cadence=1&tickDuration=1h&persist=true&startDate=2026-07-01T00:00:00.000Z"
```

Laisse tourner environ 2 minutes. Le scénario utile va jusqu'au tick 104.

Puis stoppe :

```bash
curl -X POST http://localhost:3333/sim/stop
```

## 3. Vérifier Que Les Températures Dépassent 70C

```bash
docker compose -f docker-compose.yaml exec -T timescaledb \
  psql -U tsuser -d tsdb \
  -c "SELECT MAX(sd.value) AS max_cpu_temp
      FROM sensor_data sd
      JOIN sensor s ON s.sensor_id = sd.sensor_id
      WHERE s.sensor_type = 'CPU_TEMP'
        AND sd.time >= '2026-07-01T00:00:00Z';"
```

Le résultat attendu doit être supérieur ou égal à `70`.

## 4. Exporter Le CSV ML

```bash
docker compose -f docker-compose.yaml exec -T timescaledb \
  psql -U tsuser -d tsdb \
  -c "\COPY (
    SELECT
      sd.time,
      s.server_id,
      srv.hostname,
      MAX(CASE WHEN s.sensor_type = 'CPU_TEMP' THEN sd.value END) AS cpu_temp,
      MAX(CASE WHEN s.sensor_type = 'LOAD' THEN sd.value END) AS cpu_load,
      MAX(CASE WHEN s.sensor_type = 'TOTAL_POWER' THEN sd.value END) AS total_power,
      MAX(CASE WHEN s.sensor_type = 'FAN_SPEED_1' THEN sd.value END) AS fan_speed_1,
      MAX(CASE WHEN s.sensor_type = 'FAN_SPEED_2' THEN sd.value END) AS fan_speed_2
    FROM sensor_data sd
    JOIN sensor s ON s.sensor_id = sd.sensor_id
    JOIN server srv ON srv.server_id = s.server_id
    WHERE sd.time >= '2026-07-01T00:00:00Z'
    GROUP BY sd.time, s.server_id, srv.hostname
    ORDER BY sd.time, s.server_id
  ) TO STDOUT WITH CSV HEADER" > dataset_ml_training_overheat.csv
```

## 5. Préparer Le Dataset À Seuil 70C

```bash
.venv/bin/python ml/prepare_overheat_dataset.py \
  --input dataset_ml_training_overheat.csv \
  --output data/training_dataset_overheat_70.csv \
  --threshold 70 \
  --horizon 4 \
  --run-id sc_training_overheat_2026_07_01
```

Il faut vérifier la ligne :

```text
Positive labels: ...
```

Elle doit être supérieure à `0`.

## 6. Générer Plusieurs Runs Distincts

Après rebuild, lance les scénarios suivants un par un. Laisse tourner environ 2
minutes à chaque fois, puis stoppe.

```bash
curl -X POST "http://localhost:3333/sim/scenarios/sc_training_fan_loss_overheat?cadence=1&tickDuration=1h&persist=true&startDate=2026-08-01T00:00:00.000Z"
curl -X POST http://localhost:3333/sim/stop
```

```bash
curl -X POST "http://localhost:3333/sim/scenarios/sc_training_load_wave_overheat?cadence=1&tickDuration=1h&persist=true&startDate=2026-09-01T00:00:00.000Z"
curl -X POST http://localhost:3333/sim/stop
```

```bash
curl -X POST "http://localhost:3333/sim/scenarios/sc_training_recovery_boundary?cadence=1&tickDuration=1h&persist=true&startDate=2026-10-01T00:00:00.000Z"
curl -X POST http://localhost:3333/sim/stop
```

Vérifie ensuite les températures max par mois :

```bash
docker compose -f docker-compose.yaml exec -T timescaledb \
  psql -U tsuser -d tsdb \
  -c "SELECT
        DATE_TRUNC('month', sd.time) AS month,
        MAX(sd.value) AS max_cpu_temp,
        COUNT(*) AS rows
      FROM sensor_data sd
      JOIN sensor s ON s.sensor_id = sd.sensor_id
      WHERE s.sensor_type = 'CPU_TEMP'
        AND sd.time >= '2026-07-01T00:00:00Z'
      GROUP BY 1
      ORDER BY 1;"
```

## 7. Combiner Les Datasets Préparés

Après export et préparation de chaque mois, combine les fichiers :

```bash
.venv/bin/python ml/combine_training_datasets.py \
  --output data/training_dataset_overheat_70_combined.csv \
  data/training_dataset_overheat_70.csv \
  data/training_dataset_overheat_70_august.csv \
  data/training_dataset_overheat_70_september.csv \
  data/training_dataset_overheat_70_october.csv
```

Puis entraîne :

```bash
.venv/bin/python ml/train_overheat_model.py \
  --input data/training_dataset_overheat_70_combined.csv \
  --output models/overheat_risk_model.joblib
```
