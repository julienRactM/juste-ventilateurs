#!/usr/bin/env bash
set -euo pipefail

API_BASE_URL="${API_BASE_URL:-http://localhost:3333}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yaml}"
CADENCE="${CADENCE:-1}"
TICK_DURATION="${TICK_DURATION:-1h}"
RUN_SECONDS="${RUN_SECONDS:-300}"

SCENARIOS=(
  "sc_training_overheat|2026-07-01T00:00:00.000Z"
  "sc_training_fan_loss_overheat|2026-08-01T00:00:00.000Z"
  "sc_training_load_wave_overheat|2026-09-01T00:00:00.000Z"
  "sc_training_recovery_boundary|2026-10-01T00:00:00.000Z"
)

echo "API: ${API_BASE_URL}"
echo "Cadence: ${CADENCE}s per tick"
echo "Tick duration: ${TICK_DURATION}"
echo "Run duration: ${RUN_SECONDS}s per scenario"
echo

for item in "${SCENARIOS[@]}"; do
  IFS="|" read -r scenario_id start_date <<< "${item}"
  echo "Starting ${scenario_id} at ${start_date}"

  curl -fsS -X POST \
    "${API_BASE_URL}/sim/scenarios/${scenario_id}?cadence=${CADENCE}&tickDuration=${TICK_DURATION}&persist=true&startDate=${start_date}"
  echo

  echo "Waiting ${RUN_SECONDS}s..."
  sleep "${RUN_SECONDS}"

  echo "Stopping ${scenario_id}"
  curl -fsS -X POST "${API_BASE_URL}/sim/stop"
  echo
  echo
done

echo "CPU temperature summary for training months:"
docker compose -f "${COMPOSE_FILE}" exec -T timescaledb \
  psql -U tsuser -d tsdb \
  -c "SELECT
        DATE_TRUNC('month', sd.time) AS month,
        MAX(sd.value) AS max_cpu_temp,
        COUNT(*) AS rows
      FROM sensor_data sd
      JOIN sensor s ON s.sensor_id = sd.sensor_id
      WHERE s.sensor_type = 'CPU_TEMP'
        AND sd.time >= '2026-07-01T00:00:00Z'
        AND sd.time < '2026-11-01T00:00:00Z'
      GROUP BY 1
      ORDER BY 1;"
