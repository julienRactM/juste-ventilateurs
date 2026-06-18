#!/usr/bin/env bash
set -euo pipefail

API_BASE_URL="${API_BASE_URL:-http://localhost:3333}"
SCENARIO_ID="${SCENARIO_ID:-sc_agent_maintenance_test}"
MODEL_PATH="${MODEL_PATH:-models/overheat_risk_model_h12.joblib}"
LOG_DIR="${LOG_DIR:-logs}"
RUN_SECONDS="${RUN_SECONDS:-300}"
AGENT_WARMUP_SECONDS="${AGENT_WARMUP_SECONDS:-30}"
START_DATE="${START_DATE:-2027-05-01T00:00:00.000Z}"

AGENT_PID=""

cleanup() {
  if [[ -n "${AGENT_PID}" ]] && kill -0 "${AGENT_PID}" 2>/dev/null; then
    echo "Stopping agent process ${AGENT_PID}"
    kill "${AGENT_PID}" 2>/dev/null || true
    wait "${AGENT_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

require_api() {
  curl -fsS "${API_BASE_URL}/fans" >/dev/null
}

reset_fans() {
  echo "Resetting all fans to 35%"
  for id in $(seq 1 40); do
    curl -fsS -X PATCH "${API_BASE_URL}/fans/${id}" \
      -H "Content-Type: application/json" \
      -d '{"speed_percent":35,"control_mode":"AUTO","status":"ON"}' >/dev/null
  done
}

start_agent() {
  mkdir -p "${LOG_DIR}"
  local log_file="${LOG_DIR}/maintenance_agent.log"

  echo "Starting maintenance-aware ML agent"
  echo "Agent log: ${log_file}"

  .venv/bin/python agents/overheat_model_agent.py \
    --model "${MODEL_PATH}" \
    --low-threshold 0.15 \
    --medium-threshold 0.30 \
    --high-threshold 0.45 \
    --maintenance-temp-threshold 74 \
    --maintenance-cooldown-seconds 60 > "${log_file}" 2>&1 &

  AGENT_PID="$!"
  sleep "${AGENT_WARMUP_SECONDS}"
}

echo "API: ${API_BASE_URL}"
echo "Scenario: ${SCENARIO_ID}"
echo "Start date: ${START_DATE}"
echo

require_api
reset_fans
start_agent

echo "Starting maintenance test scenario"
curl -fsS -X POST \
  "${API_BASE_URL}/sim/scenarios/${SCENARIO_ID}?cadence=1&tickDuration=1h&persist=true&startDate=${START_DATE}"
echo

echo "Waiting ${RUN_SECONDS}s"
sleep "${RUN_SECONDS}"

echo "Stopping simulation"
curl -fsS -X POST "${API_BASE_URL}/sim/stop"
echo

cleanup
AGENT_PID=""

echo
echo "Maintenance-related agent logs:"
grep -E '\[PRED\]|\[ACTION\]|\[ACTION-SKIP\]|\[MAINTENANCE\]|maintenance' "${LOG_DIR}/maintenance_agent.log" | tail -n 80 || true

echo
echo "Current fan 9 state:"
curl -fsS "${API_BASE_URL}/fans" | tr '{' '\n' | grep '"fan_id":9' || true
