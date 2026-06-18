#!/usr/bin/env bash
set -euo pipefail

API_BASE_URL="${API_BASE_URL:-http://localhost:3333}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yaml}"
SCENARIO_ID="${SCENARIO_ID:-sc_agent_control_test}"
CADENCE="${CADENCE:-1}"
TICK_DURATION="${TICK_DURATION:-1h}"
RUN_SECONDS="${RUN_SECONDS:-360}"
AGENT_WARMUP_SECONDS="${AGENT_WARMUP_SECONDS:-140}"
MODEL_PATH="${MODEL_PATH:-models/overheat_risk_model_h12.joblib}"
AGENT_LOG_DIR="${AGENT_LOG_DIR:-logs}"
CLEAN_BENCHMARK_DATA="${CLEAN_BENCHMARK_DATA:-true}"
AGENT_LOW_THRESHOLD="${AGENT_LOW_THRESHOLD:-0.15}"
AGENT_MEDIUM_THRESHOLD="${AGENT_MEDIUM_THRESHOLD:-0.30}"
AGENT_HIGH_THRESHOLD="${AGENT_HIGH_THRESHOLD:-0.45}"

RUN_35_START="${RUN_35_START:-2027-01-01T00:00:00.000Z}"
RUN_100_START="${RUN_100_START:-2027-02-01T00:00:00.000Z}"
RUN_AGENT_START="${RUN_AGENT_START:-2027-03-01T00:00:00.000Z}"
RUN_35_END="${RUN_35_END:-2027-02-01T00:00:00.000Z}"
RUN_100_END="${RUN_100_END:-2027-03-01T00:00:00.000Z}"
RUN_AGENT_END="${RUN_AGENT_END:-2027-04-01T00:00:00.000Z}"

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
  if ! curl -fsS "${API_BASE_URL}/fans" >/dev/null; then
    echo "ERROR: API is not reachable on ${API_BASE_URL}, or /fans is unavailable."
    echo "Check the stack with:"
    echo "  docker compose -f ${COMPOSE_FILE} ps"
    echo "  docker compose -f ${COMPOSE_FILE} logs --tail=80 api-node"
    exit 1
  fi
}

ensure_no_external_agent() {
  local current_pid
  current_pid="$$"
  if pgrep -f "agents/overheat_model_agent.py" | grep -v "^${current_pid}$" >/dev/null; then
    echo "ERROR: an overheat_model_agent.py process already seems to be running."
    echo "Stop it before running this benchmark, otherwise the fixed-fan baselines will be polluted."
    exit 1
  fi
}

reset_fans() {
  local speed="$1"
  echo "Setting all fans to ${speed}%"
  for id in $(seq 1 40); do
    curl -fsS -X PATCH "${API_BASE_URL}/fans/${id}" \
      -H "Content-Type: application/json" \
      -d "{\"speed_percent\":${speed},\"control_mode\":\"AUTO\",\"status\":\"ON\"}" >/dev/null
  done
}

show_fans() {
  curl -fsS "${API_BASE_URL}/fans" \
    | grep -o '"speed_percent":[0-9]*' \
    | sort \
    | uniq -c
}

clean_benchmark_data() {
  if [[ "${CLEAN_BENCHMARK_DATA}" != "true" ]]; then
    return
  fi

  echo "Cleaning previous benchmark sensor_data from 2027-01-01 to 2027-04-01"
  docker compose -f "${COMPOSE_FILE}" exec -T timescaledb \
    psql -U tsuser -d tsdb \
    -c "DELETE FROM sensor_data
        WHERE time >= TIMESTAMPTZ '2027-01-01T00:00:00Z'
          AND time <  TIMESTAMPTZ '2027-04-01T00:00:00Z';"
}

start_scenario() {
  local strategy="$1"
  local start_date="$2"

  echo
  echo "============================================================"
  echo "Starting benchmark run: ${strategy}"
  echo "Scenario: ${SCENARIO_ID}"
  echo "Start date: ${start_date}"
  echo "Duration: ${RUN_SECONDS}s"
  echo "============================================================"

  curl -fsS -X POST \
    "${API_BASE_URL}/sim/scenarios/${SCENARIO_ID}?cadence=${CADENCE}&tickDuration=${TICK_DURATION}&persist=true&startDate=${start_date}"
  echo

  sleep "${RUN_SECONDS}"

  echo "Stopping simulation"
  curl -fsS -X POST "${API_BASE_URL}/sim/stop"
  echo

  echo "Fan distribution after ${strategy}:"
  show_fans
}

start_agent() {
  mkdir -p "${AGENT_LOG_DIR}"
  local log_file="${AGENT_LOG_DIR}/benchmark_agent_ml.log"

  echo "Starting ML agent in background"
  echo "Agent log: ${log_file}"

  .venv/bin/python agents/overheat_model_agent.py \
    --model "${MODEL_PATH}" \
    --low-threshold "${AGENT_LOW_THRESHOLD}" \
    --medium-threshold "${AGENT_MEDIUM_THRESHOLD}" \
    --high-threshold "${AGENT_HIGH_THRESHOLD}" > "${log_file}" 2>&1 &

  AGENT_PID="$!"
  sleep 5

  if ! kill -0 "${AGENT_PID}" 2>/dev/null; then
    echo "ERROR: ML agent failed to start. Last log lines:"
    tail -n 40 "${log_file}" || true
    exit 1
  fi

  echo "Waiting ${AGENT_WARMUP_SECONDS}s so the agent can collect MQTT history before the ML run"
  sleep "${AGENT_WARMUP_SECONDS}"
}

stop_agent() {
  if [[ -n "${AGENT_PID}" ]] && kill -0 "${AGENT_PID}" 2>/dev/null; then
    echo "Stopping ML agent"
    kill "${AGENT_PID}" 2>/dev/null || true
    wait "${AGENT_PID}" 2>/dev/null || true
    AGENT_PID=""
  fi
}

print_summary_sql() {
  echo
  echo "============================================================"
  echo "Benchmark summary"
  echo "============================================================"

  docker compose -f "${COMPOSE_FILE}" exec -T timescaledb \
    psql -U tsuser -d tsdb \
    -c "WITH runs(strategy, start_time, end_time) AS (
          VALUES
            ('fixed_35',  TIMESTAMPTZ '${RUN_35_START}', TIMESTAMPTZ '${RUN_35_END}'),
            ('fixed_100', TIMESTAMPTZ '${RUN_100_START}', TIMESTAMPTZ '${RUN_100_END}'),
            ('agent_ml',  TIMESTAMPTZ '${RUN_AGENT_START}', TIMESTAMPTZ '${RUN_AGENT_END}')
        ),
        cpu AS (
          SELECT
            r.strategy,
            COUNT(*) AS cpu_points,
            ROUND(MIN(sd.value)::numeric, 2) AS min_cpu_temp,
            ROUND(AVG(sd.value)::numeric, 2) AS avg_cpu_temp,
            ROUND(MAX(sd.value)::numeric, 2) AS max_cpu_temp,
            COUNT(*) FILTER (WHERE sd.value >= 70) AS cpu_points_over_70,
            COUNT(*) FILTER (WHERE sd.value >= 78) AS cpu_points_over_78
          FROM runs r
          JOIN sensor_data sd ON sd.time >= r.start_time AND sd.time < r.end_time
          JOIN sensor s ON s.sensor_id = sd.sensor_id
          WHERE s.sensor_type = 'CPU_TEMP'
          GROUP BY r.strategy
        ),
        fan AS (
          SELECT
            r.strategy,
            ROUND(AVG(sd.value)::numeric, 2) AS avg_fan_speed,
            ROUND(MAX(sd.value)::numeric, 2) AS max_fan_speed
          FROM runs r
          JOIN sensor_data sd ON sd.time >= r.start_time AND sd.time < r.end_time
          JOIN sensor s ON s.sensor_id = sd.sensor_id
          WHERE s.sensor_type LIKE 'FAN_SPEED_%'
          GROUP BY r.strategy
        ),
        power AS (
          SELECT
            r.strategy,
            ROUND(AVG(sd.value)::numeric, 2) AS avg_server_power_w,
            ROUND(MAX(sd.value)::numeric, 2) AS max_server_power_w
          FROM runs r
          JOIN sensor_data sd ON sd.time >= r.start_time AND sd.time < r.end_time
          JOIN sensor s ON s.sensor_id = sd.sensor_id
          WHERE s.sensor_type = 'TOTAL_POWER'
          GROUP BY r.strategy
        )
        SELECT
          cpu.strategy,
          cpu.cpu_points,
          cpu.min_cpu_temp,
          cpu.avg_cpu_temp,
          cpu.max_cpu_temp,
          cpu.cpu_points_over_70,
          cpu.cpu_points_over_78,
          fan.avg_fan_speed,
          fan.max_fan_speed,
          power.avg_server_power_w,
          power.max_server_power_w
        FROM cpu
        LEFT JOIN fan USING (strategy)
        LEFT JOIN power USING (strategy)
        ORDER BY CASE cpu.strategy
          WHEN 'fixed_35' THEN 1
          WHEN 'fixed_100' THEN 2
          WHEN 'agent_ml' THEN 3
          ELSE 4
        END;"

  echo
  echo "Agent log excerpts:"
  if [[ -f "${AGENT_LOG_DIR}/benchmark_agent_ml.log" ]]; then
    grep -E '\[PRED\]|\[ACTION\]|\[MAINTENANCE\]' "${AGENT_LOG_DIR}/benchmark_agent_ml.log" | tail -n 30 || true
  else
    echo "No agent log found."
  fi
}

echo "API: ${API_BASE_URL}"
echo "Compose file: ${COMPOSE_FILE}"
echo "Scenario: ${SCENARIO_ID}"
echo "Model: ${MODEL_PATH}"
echo "Agent thresholds: low=${AGENT_LOW_THRESHOLD}, medium=${AGENT_MEDIUM_THRESHOLD}, high=${AGENT_HIGH_THRESHOLD}"
echo

require_api
ensure_no_external_agent

if [[ ! -f "${MODEL_PATH}" ]]; then
  echo "ERROR: model not found: ${MODEL_PATH}"
  exit 1
fi

echo "Initial fan distribution:"
show_fans

clean_benchmark_data

reset_fans 35
start_scenario "fixed_35" "${RUN_35_START}"

reset_fans 100
start_scenario "fixed_100" "${RUN_100_START}"

reset_fans 35
start_agent
start_scenario "agent_ml" "${RUN_AGENT_START}"
stop_agent

reset_fans 35
print_summary_sql

echo
echo "Benchmark complete."
