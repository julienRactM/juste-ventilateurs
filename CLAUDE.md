# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

This is a datacenter IoT simulation platform used as a training exercise. Students build a Python MQTT consumer + AI fan controller that connects to this digital twin. The platform simulates server thermal behavior in real time and exposes a REST API for fan control.

## Architecture Overview

```
docker-compose.yaml
├── kafka              — Kafka broker in KRaft mode (port 9092, no ZooKeeper)
├── akhq               — Kafka UI (port 9999)
├── timescaledb        — TimescaleDB/PostgreSQL (port 5432) for time-series sensor data
├── api-node           — Fastify REST API + physics engine (port 3333)
├── mqtt-producer      — Reads DB state, publishes telemetry to the Kafka "telemetry" topic
├── mqtt-consumer      — Consumes "telemetry" topic and persists sensor readings to TimescaleDB
├── grafana            — Dashboards pre-wired to TimescaleDB (port 3000)
└── pgadmin            — DB admin UI (port 5050, admin@example.com / admin)
```

### nodejs-server (TypeScript/Fastify)

Source is in `src/`, compiled output in `dist/`. The server is structured as Fastify plugins:

- **`src/server.ts`** — entry point; registers all controllers, Socket.IO, Swagger
- **`src/simulation/`** — tick-based physics engine (`SimulationService`); advances virtual clock and computes thermal/load state
- **`src/scenario/`** — `ScenarioService` loads JSON scenarios from `src/data_seed/scenarios.json` and injects faults on specific ticks (CRASH_FAN, LOAD_SPIKE_ALL, THERMAL_DRIFT_SERVER, etc.)
- **`src/mqtt/producer.ts`** — standalone process; polls `/internal/cadence`, fetches live weather from OpenWeatherMap, computes thermal physics per server, publishes to MQTT
- **`src/mqtt/stateless-consumer.ts`** — standalone process; subscribes to MQTT and writes `sensor_data` rows to DB
- **`src/seed/`** — topology builder; creates ClusterLocation → Cluster → Server → Sensor + Fan hierarchy from profiles (SMALL/MEDIUM/BIG defined in `src/data_seed/seed_cluster_configuration.ts`)
- **`src/hardware/`** — `PATCH /fans/:id` endpoint; directly updates fan speed/status/mode in DB
- **`src/analytics/`** — PUE and energy cost calculations
- **`src/agent/`** — SRE agent endpoint
- **`src/prisma/schema.prisma`** — canonical DB schema; two schema files exist (one root, one in `generated/`), the root one is the source of truth

### Data Flow

```
SimulationService (tick) → ScenarioService (fault injection)
        ↓
mqtt-producer (physics + OpenWeather) → Mosquitto → mqtt-consumer → TimescaleDB
        ↑                                                                 ↓
  /internal/cadence                                               Grafana dashboards
        ↑
  PATCH /fans/:id  ← Student Python agent subscribes to v1/gateway/telemetry/#
  POST /sim/maintenance/repair
```

### Key API Routes

| Method | Path | Description |
|--------|------|-------------|
| POST | `/sim/tick` | Advance simulation by one tick |
| POST | `/sim/scenarios/marseille` | Trigger Marseille crisis scenario |
| POST | `/sim/maintenance/repair` | Send repair crew to a fan |
| PATCH | `/fans/:id` | Update fan speed/status |
| GET | `/docs` | Swagger UI |
| GET | `/dashboard` | Live dashboard |
| GET | `/create` | Topology builder UI |

### Environment Variables

- `DATABASE_URL` — PostgreSQL connection string (all Node containers)
- `KAFKA_BROKERS` — comma-separated broker list, e.g. `kafka-broker:9092` (producer and consumer)
- `OPENWEATHER_API_KEY` — required by `mqtt-producer` for live city weather

## Commands

### Start the stack
```bash
docker compose up -d --build
```

### Build the Node server only
```bash
cd nodejs-server
npm run build
```

### Run a specific service
```bash
docker compose up -d api-node
docker compose logs -f api-node
```

### Seed telemetry data
```bash
cd nodejs-server
npm run seed:telemetry
```

### Access services
- API + Swagger: http://localhost:3333/docs
- Dashboard: http://localhost:3333/dashboard
- Topology builder: http://localhost:3333/create
- Grafana: http://localhost:3000 (admin/admin)
- pgAdmin: http://localhost:5050 (admin@example.com/admin)
- AKHQ (Kafka UI): http://localhost:9999

## Student Exercise Context

Students implement `correction_2.py` (root level) as their reference. The agent:
1. Subscribes to `v1/gateway/telemetry/#`
2. Extracts `CPU_TEMP`, `LOAD`, `FAN_SPEED_*` sensor values
3. Applies a proportional controller (`Kp * error + Kf * load`) for nominal regulation via `PATCH /fans/:id`
4. Detects crisis (temp ≥ 78°C) and calls `POST /sim/maintenance/repair` with the fan ID

The evaluation scenario (`POST /sim/scenarios/marseille?cadence=1&persist=true`) triggers:
- Tick 52: CPU load ×2
- Tick 56: +15°C thermal drift on worker-01
- Tick 60: CRASH_FAN on worker-01

## Database Notes

TimescaleDB credentials: `tsuser` / `tspassword` / `tsdb`. The `sensor_data` table is a hypertable partitioned by `time`. Fan state lives in the `fan` table; the MQTT producer reads fan speeds from there each tick to compute cooling physics.
