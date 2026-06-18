#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yaml}"
PYTHON="${PYTHON:-.venv/bin/python}"
THRESHOLD="${THRESHOLD:-70}"
HORIZON="${HORIZON:-4}"

mkdir -p data models

RUNS=(
  "2026-07-01T00:00:00Z|2026-08-01T00:00:00Z|sc_training_overheat_2026_07|dataset_ml_training_overheat_july.csv|data/training_dataset_overheat_70_july.csv"
  "2026-08-01T00:00:00Z|2026-09-01T00:00:00Z|sc_training_fan_loss_2026_08|dataset_ml_training_overheat_august.csv|data/training_dataset_overheat_70_august.csv"
  "2026-09-01T00:00:00Z|2026-10-01T00:00:00Z|sc_training_load_wave_2026_09|dataset_ml_training_overheat_september.csv|data/training_dataset_overheat_70_september.csv"
  "2026-10-01T00:00:00Z|2026-11-01T00:00:00Z|sc_training_recovery_2026_10|dataset_ml_training_overheat_october.csv|data/training_dataset_overheat_70_october.csv"
)

prepared_files=()

for item in "${RUNS[@]}"; do
  IFS="|" read -r start_date end_date run_id raw_csv prepared_csv <<< "${item}"

  echo "Exporting ${run_id}: ${start_date} -> ${end_date}"
  docker compose -f "${COMPOSE_FILE}" exec -T timescaledb \
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
      WHERE sd.time >= '${start_date}'
        AND sd.time < '${end_date}'
      GROUP BY sd.time, s.server_id, srv.hostname
      ORDER BY sd.time, s.server_id
    ) TO STDOUT WITH CSV HEADER" > "${raw_csv}"

  echo "Preparing ${prepared_csv}"
  "${PYTHON}" ml/prepare_overheat_dataset.py \
    --input "${raw_csv}" \
    --output "${prepared_csv}" \
    --threshold "${THRESHOLD}" \
    --horizon "${HORIZON}" \
    --run-id "${run_id}"

  prepared_files+=("${prepared_csv}")
  echo
done

combined_dataset="data/training_dataset_overheat_70_combined.csv"
echo "Combining prepared datasets into ${combined_dataset}"
"${PYTHON}" ml/combine_training_datasets.py \
  --output "${combined_dataset}" \
  "${prepared_files[@]}"

echo
echo "Training overheat model"
"${PYTHON}" ml/train_overheat_model.py \
  --input "${combined_dataset}" \
  --output models/overheat_risk_model.joblib
