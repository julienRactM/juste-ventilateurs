from pathlib import Path
from datetime import datetime
import json
import os

import pandas as pd
import paho.mqtt.client as mqtt

MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "v1/gateway/telemetry/#"
DATA_FILE = Path("data/telemetry_history.csv")

DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

def to_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

def get_sensor_value(payload, sensor_type):
    sensors = payload.get("sensors", [])
    for sensor in sensors:
        if sensor.get("type") == sensor_type:
            return to_float(sensor.get("value"))
    return None

def save_to_csv(data):
    df = pd.DataFrame([data])
    file_exists = DATA_FILE.exists()
    df.to_csv(DATA_FILE, mode="a", index=False, header=not file_exists)

def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        print(f"✅ Connecté au broker MQTT ({MQTT_BROKER}:{MQTT_PORT})")
        client.subscribe(MQTT_TOPIC, qos=0)
    else:
        print(f"❌ Erreur de connexion MQTT : {reason_code}")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())

        data = {
            "timestamp": payload.get("timestamp", datetime.now().isoformat()),
            "machine": payload.get("hostname"),
            "load": to_float(payload.get("load_percent")),
            "temp": get_sensor_value(payload, "CPU_TEMP"),
            "fan_speed": to_float(payload.get("current_fan_speed")),
            "power": get_sensor_value(payload, "TOTAL_POWER"),
        }

        print(
            f"📥 {data['machine']} | "
            f"T={data['temp']}°C | "
            f"L={data['load']}% | "
            f"Fan={data['fan_speed']}%"
        )

        save_to_csv(data)

    except Exception as e:
        print(f"⚠️ Erreur parsing message MQTT : {e}")

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message

try:
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_forever()
except KeyboardInterrupt:
    print("\n🛑 Arrêt du subscriber.")
    client.disconnect()