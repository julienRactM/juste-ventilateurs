from pathlib import Path
from datetime import datetime
import json
import os
import sys
import traceback

import pandas as pd
import paho.mqtt.client as mqtt
import psycopg
from psycopg.types.json import Jsonb
import requests


MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "v1/gateway/telemetry/#")

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:3333")
FAN_PATCH_PATH = os.getenv("FAN_PATCH_PATH", "/fans/{id}")

TARGET_TEMP_C = float(os.getenv("TARGET_TEMP_C", "60.0"))
KP = float(os.getenv("KP", "4.0"))
FAN_MIN = int(os.getenv("FAN_MIN", "25"))
FAN_MAX = int(os.getenv("FAN_MAX", "100"))
MIN_DELTA = float(os.getenv("MIN_DELTA", "2.0"))

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
DEFAULT_FAN_ID = int(os.getenv("FAN_ID", "1"))

DATA_FILE = Path("data/telemetry_history.csv")
DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})

TS_DB_NAME = os.getenv("TS_DB_NAME", "tsdb")
TS_DB_USER = os.getenv("TS_DB_USER", "tsuser")
TS_DB_PASSWORD = os.getenv("TS_DB_PASSWORD", "tspassword")
TS_DB_HOST = os.getenv("TS_DB_HOST", "localhost")
TS_DB_PORT = int(os.getenv("TS_DB_PORT", "5432"))

TS_CONN_STR = (
    f"dbname={TS_DB_NAME} user={TS_DB_USER} password={TS_DB_PASSWORD} "
    f"host={TS_DB_HOST} port={TS_DB_PORT}"
)

TS_CONN = psycopg.connect(TS_CONN_STR, autocommit=True)
TS_CUR = TS_CONN.cursor()


def to_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def get_sensor_value(payload, sensor_type):
    for sensor in payload.get("sensors", []):
        if sensor.get("type") == sensor_type:
            return to_float(sensor.get("value"))
    return None


def parse_hostname(hostname: str):
    if not hostname:
        return None, None, None

    parts = hostname.split("-")
    city = parts[0] if len(parts) > 0 else None
    cluster = "-".join(parts[:4]) if len(parts) >= 4 else None
    role = "master" if "master" in parts else "worker" if "worker" in parts else None
    return cluster, role, city


def save_to_csv(data):
    df = pd.DataFrame([data])
    file_exists = DATA_FILE.exists()
    df.to_csv(DATA_FILE, mode="a", index=False, header=not file_exists)


def insert_into_timescaledb(row):
    sql = """
        INSERT INTO telemetry_normalized (
            ts, cluster, machine, role, city,
            load_percent, cpu_temp_c, fan_speed_percent,
            total_power_w, ambient_dc_temp_c, external_temp_c,
            event_type, raw_payload
        )
        VALUES (%(ts)s, %(cluster)s, %(machine)s, %(role)s, %(city)s,
                %(load_percent)s, %(cpu_temp_c)s, %(fan_speed_percent)s,
                %(total_power_w)s, %(ambient_dc_temp_c)s, %(external_temp_c)s,
                %(event_type)s, %(raw_payload)s)
    """
    db_row = row.copy()
    db_row["raw_payload"] = Jsonb(db_row["raw_payload"])
    TS_CUR.execute(sql, db_row)


def compute_fan_speed(temp_c, current_speed):
    if temp_c is None or current_speed is None:
        return current_speed

    error = temp_c - TARGET_TEMP_C
    delta = KP * error
    new_speed = max(FAN_MIN, min(FAN_MAX, current_speed + delta))

    if abs(new_speed - current_speed) < MIN_DELTA:
        return current_speed

    return round(new_speed, 1)


def send_fan_command(fan_id, new_speed):
    url = API_BASE_URL.rstrip("/") + FAN_PATCH_PATH.format(id=fan_id)
    payload = {"speed_percent": new_speed}

    if DRY_RUN:
        print(f"🧪 DRY-RUN PATCH {url} -> {payload}")
        return

    try:
        resp = SESSION.patch(url, json=payload, timeout=5)
        resp.raise_for_status()
        print(f"✅ PATCH {url} -> {payload} (status={resp.status_code})")
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Erreur PATCH ventilateur : {e}", file=sys.stderr)


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        print(f"✅ Connecté au broker MQTT ({MQTT_BROKER}:{MQTT_PORT})")
        client.subscribe(MQTT_TOPIC, qos=0)
    else:
        print(f"❌ Erreur de connexion MQTT : {reason_code}")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())

        hostname = payload.get("hostname")
        cluster, role, city = parse_hostname(hostname)

        ts = payload.get("timestamp", datetime.now().isoformat())
        temp = get_sensor_value(payload, "CPU_TEMP")
        power = get_sensor_value(payload, "TOTAL_POWER")
        load = to_float(payload.get("load_percent"))
        fan_speed = to_float(payload.get("current_fan_speed"))
        ambient_dc_temp = to_float(payload.get("environment", {}).get("ambient_dc_temp"))
        external_temp = to_float(payload.get("environment", {}).get("external_temp"))

        row = {
            "ts": ts,
            "cluster": cluster,
            "machine": hostname,
            "role": role,
            "city": city,
            "load_percent": load,
            "cpu_temp_c": temp,
            "fan_speed_percent": fan_speed,
            "total_power_w": power,
            "ambient_dc_temp_c": ambient_dc_temp,
            "external_temp_c": external_temp,
            "event_type": None,
            "raw_payload": payload,
        }

        print(f"📥 {hostname} | T={temp}°C | L={load}% | Fan={fan_speed}%")

        save_to_csv({**row, "raw_payload": json.dumps(payload)})
        insert_into_timescaledb(row)

        if fan_speed is not None and temp is not None:
            new_speed = compute_fan_speed(temp, fan_speed)
            if new_speed != fan_speed:
                print(f"🎛️ Ajustement ventilateur {DEFAULT_FAN_ID} : {fan_speed}% -> {new_speed}%")
                send_fan_command(DEFAULT_FAN_ID, new_speed)

    except Exception as e:
        print(f"⚠️ Erreur parsing / stockage / contrôle : {e}", file=sys.stderr)
        traceback.print_exc()


def on_log(client, userdata, level, buf):
    if level == mqtt.MQTT_LOG_ERR:
        print(f"MQTT LOG ERROR: {buf}", file=sys.stderr)


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_log = on_log

    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    print("🚀 Agent live local avec TimescaleDB (DRY_RUN =", DRY_RUN, ")")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n🛑 Arrêt de l’agent.")
    finally:
        client.disconnect()
        TS_CUR.close()
        TS_CONN.close()


if __name__ == "__main__":
    main()