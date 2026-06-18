#!/usr/bin/env bash
set -euo pipefail

API_BASE_URL="${API_BASE_URL:-http://localhost:3333}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yaml}"
PYTHON="${PYTHON:-.venv/bin/python}"
RUN_SECONDS="${RUN_SECONDS:-95}"

START_DATE="2026-11-01T00:00:00.000Z"
START_SQL="2026-11-01T00:00:00Z"
END_SQL="2026-11-08T00:00:00Z"
RAW_CSV="dataset_ml_eval_unseen_overheat_clean.csv"
PREPARED_CSV="data/eval_dataset_unseen_overheat_70_h12_clean.csv"
MODEL_PATH="models/overheat_risk_model_h12.joblib"

echo "Cleaning previous November evaluation data..."
docker compose -f "${COMPOSE_FILE}" exec -T timescaledb \
  psql -U tsuser -d tsdb \
  -c "DELETE FROM sensor_data
      WHERE time >= '${START_SQL}'
        AND time < '2026-12-01T00:00:00Z';"

echo "Starting sc_eval_unseen_overheat..."
curl -fsS -X POST \
  "${API_BASE_URL}/sim/scenarios/sc_eval_unseen_overheat?cadence=1&tickDuration=1h&persist=true&startDate=${START_DATE}"
echo

echo "Waiting ${RUN_SECONDS}s..."
sleep "${RUN_SECONDS}"

echo "Stopping simulation..."
curl -fsS -X POST "${API_BASE_URL}/sim/stop"
echo

echo "Checking max CPU temperature..."
docker compose -f "${COMPOSE_FILE}" exec -T timescaledb \
  psql -U tsuser -d tsdb \
  -c "SELECT
        MAX(sd.value) AS max_cpu_temp,
        COUNT(*) FILTER (WHERE sd.value >= 70) AS over_70
      FROM sensor_data sd
      JOIN sensor s ON s.sensor_id = sd.sensor_id
      WHERE s.sensor_type = 'CPU_TEMP'
        AND sd.time >= '${START_SQL}'
        AND sd.time < '${END_SQL}';"

echo "Exporting evaluation CSV..."
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
    WHERE sd.time >= '${START_SQL}'
      AND sd.time < '${END_SQL}'
    GROUP BY sd.time, s.server_id, srv.hostname
    ORDER BY sd.time, s.server_id
  ) TO STDOUT WITH CSV HEADER" > "${RAW_CSV}"

echo "Preparing evaluation dataset with horizon 12..."
"${PYTHON}" ml/prepare_overheat_dataset.py \
  --input "${RAW_CSV}" \
  --output "${PREPARED_CSV}" \
  --threshold 70 \
  --horizon 12 \
  --run-id sc_eval_unseen_overheat_clean_2026_11

echo "Evaluating pure model..."
"${PYTHON}" ml/evaluate_overheat_model.py \
  --model "${MODEL_PATH}" \
  --input "${PREPARED_CSV}"
