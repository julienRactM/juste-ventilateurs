#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yaml}"
PUE="${PUE:-1.56}"
ENERGY_PRICE_EUR_KWH="${ENERGY_PRICE_EUR_KWH:-0.20}"

RUN_35_START="${RUN_35_START:-2027-01-01T00:00:00.000Z}"
RUN_100_START="${RUN_100_START:-2027-02-01T00:00:00.000Z}"
RUN_AGENT_START="${RUN_AGENT_START:-2027-03-01T00:00:00.000Z}"
RUN_35_END="${RUN_35_END:-2027-02-01T00:00:00.000Z}"
RUN_100_END="${RUN_100_END:-2027-03-01T00:00:00.000Z}"
RUN_AGENT_END="${RUN_AGENT_END:-2027-04-01T00:00:00.000Z}"

echo "Green IT / PUE / ROI summary"
echo "PUE: ${PUE}"
echo "Energy price: ${ENERGY_PRICE_EUR_KWH} EUR/kWh"
echo

docker compose -f "${COMPOSE_FILE}" exec -T timescaledb \
  psql -U tsuser -d tsdb \
  -c "WITH params AS (
        SELECT ${PUE}::double precision AS pue,
               ${ENERGY_PRICE_EUR_KWH}::double precision AS energy_price_eur_kwh
      ),
      runs(strategy, start_time, end_time) AS (
        VALUES
          ('fixed_35',  TIMESTAMPTZ '${RUN_35_START}', TIMESTAMPTZ '${RUN_35_END}'),
          ('fixed_100', TIMESTAMPTZ '${RUN_100_START}', TIMESTAMPTZ '${RUN_100_END}'),
          ('agent_ml',  TIMESTAMPTZ '${RUN_AGENT_START}', TIMESTAMPTZ '${RUN_AGENT_END}')
      ),
      power AS (
        SELECT
          r.strategy,
          COUNT(*) AS server_hours,
          SUM(sd.value) / 1000.0 AS it_energy_kwh,
          AVG(sd.value) AS avg_server_power_w,
          MAX(sd.value) AS max_server_power_w
        FROM runs r
        JOIN sensor_data sd ON sd.time >= r.start_time AND sd.time < r.end_time
        JOIN sensor s ON s.sensor_id = sd.sensor_id
        WHERE s.sensor_type = 'TOTAL_POWER'
        GROUP BY r.strategy
      ),
      thermal AS (
        SELECT
          r.strategy,
          MAX(sd.value) AS max_cpu_temp,
          COUNT(*) FILTER (WHERE sd.value >= 70) AS cpu_points_over_70
        FROM runs r
        JOIN sensor_data sd ON sd.time >= r.start_time AND sd.time < r.end_time
        JOIN sensor s ON s.sensor_id = sd.sensor_id
        WHERE s.sensor_type = 'CPU_TEMP'
        GROUP BY r.strategy
      ),
      fan AS (
        SELECT
          r.strategy,
          AVG(sd.value) AS avg_fan_speed
        FROM runs r
        JOIN sensor_data sd ON sd.time >= r.start_time AND sd.time < r.end_time
        JOIN sensor s ON s.sensor_id = sd.sensor_id
        WHERE s.sensor_type LIKE 'FAN_SPEED_%'
        GROUP BY r.strategy
      ),
      metrics AS (
        SELECT
          p.strategy,
          p.server_hours,
          ROUND(p.avg_server_power_w::numeric, 2) AS avg_server_power_w,
          ROUND(p.max_server_power_w::numeric, 2) AS max_server_power_w,
          ROUND(f.avg_fan_speed::numeric, 2) AS avg_fan_speed,
          ROUND(t.max_cpu_temp::numeric, 2) AS max_cpu_temp,
          t.cpu_points_over_70,
          ROUND(p.it_energy_kwh::numeric, 2) AS it_energy_kwh,
          ROUND((p.it_energy_kwh * params.pue)::numeric, 2) AS building_energy_kwh,
          ROUND((p.it_energy_kwh * params.pue * params.energy_price_eur_kwh)::numeric, 2) AS estimated_cost_eur
        FROM power p
        JOIN thermal t USING (strategy)
        JOIN fan f USING (strategy)
        CROSS JOIN params
      )
      SELECT *
      FROM metrics
      ORDER BY CASE strategy
        WHEN 'fixed_35' THEN 1
        WHEN 'fixed_100' THEN 2
        WHEN 'agent_ml' THEN 3
        ELSE 4
      END;"

echo
echo "ROI versus fixed_100"
docker compose -f "${COMPOSE_FILE}" exec -T timescaledb \
  psql -U tsuser -d tsdb \
  -c "WITH params AS (
        SELECT ${PUE}::double precision AS pue,
               ${ENERGY_PRICE_EUR_KWH}::double precision AS energy_price_eur_kwh
      ),
      runs(strategy, start_time, end_time) AS (
        VALUES
          ('fixed_100', TIMESTAMPTZ '${RUN_100_START}', TIMESTAMPTZ '${RUN_100_END}'),
          ('agent_ml',  TIMESTAMPTZ '${RUN_AGENT_START}', TIMESTAMPTZ '${RUN_AGENT_END}')
      ),
      costs AS (
        SELECT
          r.strategy,
          SUM(sd.value) / 1000.0 * params.pue AS building_energy_kwh,
          SUM(sd.value) / 1000.0 * params.pue * params.energy_price_eur_kwh AS cost_eur
        FROM runs r
        JOIN sensor_data sd ON sd.time >= r.start_time AND sd.time < r.end_time
        JOIN sensor s ON s.sensor_id = sd.sensor_id
        CROSS JOIN params
        WHERE s.sensor_type = 'TOTAL_POWER'
        GROUP BY r.strategy, params.pue, params.energy_price_eur_kwh
      )
      SELECT
        ROUND(f.cost_eur::numeric, 2) AS fixed_100_cost_eur,
        ROUND(a.cost_eur::numeric, 2) AS agent_ml_cost_eur,
        ROUND((f.cost_eur - a.cost_eur)::numeric, 2) AS estimated_saving_eur,
        ROUND((100.0 * (f.cost_eur - a.cost_eur) / NULLIF(f.cost_eur, 0))::numeric, 2) AS saving_percent,
        ROUND(f.building_energy_kwh::numeric, 2) AS fixed_100_building_kwh,
        ROUND(a.building_energy_kwh::numeric, 2) AS agent_ml_building_kwh
      FROM costs f
      JOIN costs a ON f.strategy = 'fixed_100' AND a.strategy = 'agent_ml';"
