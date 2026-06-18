#!/usr/bin/env python3
"""MQTT agent driven by the trained overheat prediction model.

The agent consumes `v1/gateway/telemetry/#`, rebuilds the same online features
used at training time, predicts the probability of crossing 70C in the next
horizon, and controls all fans of the affected server through PATCH /fans/{id}.

This agent intentionally does not use a hard temperature rule for prediction.
The decision to accelerate fans is based on the model probability only.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import paho.mqtt.client as mqtt
import requests


DEFAULT_TOPIC = "v1/gateway/telemetry/#"


@dataclass
class Sample:
    timestamp: str
    hostname: str
    cpu_temp: float
    cpu_load: float
    total_power: float
    fan_speed_1: float
    fan_speed_2: float

    @property
    def fan_speed_mean(self) -> float:
        return (self.fan_speed_1 + self.fan_speed_2) / 2.0


def value_or_nan(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def lag(values: list[float], steps: int) -> float:
    return values[-1 - steps] if len(values) > steps else math.nan


def delta(values: list[float], steps: int) -> float:
    previous = lag(values, steps)
    current = values[-1]
    if math.isnan(previous) or math.isnan(current):
        return math.nan
    return current - previous


def rolling_mean(values: list[float], window: int) -> float:
    usable = [v for v in values[-window:] if not math.isnan(v)]
    return float(sum(usable) / len(usable)) if usable else math.nan


def rolling_std(values: list[float], window: int) -> float:
    usable = [v for v in values[-window:] if not math.isnan(v)]
    if len(usable) < 2:
        return 0.0
    avg = sum(usable) / len(usable)
    return math.sqrt(sum((v - avg) ** 2 for v in usable) / len(usable))


def slope(values: list[float], window: int) -> float:
    d = delta(values, window)
    return d / window if not math.isnan(d) else math.nan


def expected_temp_without_drift(cpu_load: float, fan_speed: float) -> float:
    if math.isnan(cpu_load) or math.isnan(fan_speed):
        return math.nan
    load_ratio = max(0.0, min(1.0, cpu_load / 100.0))
    temp = 22.0 + (load_ratio * 55.0)
    if fan_speed > 0:
        temp -= (fan_speed / 100.0) * 25.0 * (load_ratio + 0.3)
    return max(22.0, min(105.0, temp))


def estimated_ticks_to_threshold(current_temp: float, temp_slope: float, threshold_celsius: float) -> float:
    if math.isnan(current_temp) or math.isnan(temp_slope) or temp_slope <= 0:
        return 999.0
    if current_temp >= threshold_celsius:
        return 0.0
    return min(999.0, (threshold_celsius - current_temp) / temp_slope)


class OnlineFeatureBuilder:
    def __init__(self, max_history: int = 32, threshold_celsius: float = 70.0) -> None:
        self.history: dict[str, deque[Sample]] = defaultdict(lambda: deque(maxlen=max_history))
        self.threshold_celsius = threshold_celsius

    def add(self, sample: Sample) -> None:
        self.history[sample.hostname].append(sample)

    def ready(self, hostname: str) -> bool:
        return len(self.history[hostname]) >= 13

    def features(self, hostname: str) -> dict[str, float]:
        samples = list(self.history[hostname])
        cpu_temp = [s.cpu_temp for s in samples]
        cpu_load = [s.cpu_load for s in samples]
        total_power = [s.total_power for s in samples]
        fan_speed_1 = [s.fan_speed_1 for s in samples]
        fan_speed_2 = [s.fan_speed_2 for s in samples]
        fan_mean = [s.fan_speed_mean for s in samples]

        current_temp = cpu_temp[-1]
        current_load = cpu_load[-1]
        current_power = total_power[-1]
        current_fan = fan_mean[-1]

        temp_delta_1 = delta(cpu_temp, 1)
        temp_delta_3 = delta(cpu_temp, 3)
        temp_delta_6 = delta(cpu_temp, 6)
        temp_delta_12 = delta(cpu_temp, 12)
        temp_slope_3 = slope(cpu_temp, 3)
        temp_slope_6 = slope(cpu_temp, 6)
        temp_slope_12 = slope(cpu_temp, 12)
        expected_temp = expected_temp_without_drift(current_load, current_fan)
        cooling_deficit = current_temp - expected_temp if not math.isnan(expected_temp) else math.nan
        power_per_load = current_power / current_load if current_load > 0 else math.nan

        return {
            "cpu_temp": current_temp,
            "cpu_load": current_load,
            "total_power": current_power,
            "fan_speed_1": fan_speed_1[-1],
            "fan_speed_2": fan_speed_2[-1],
            "fan_speed_mean": current_fan,
            "cpu_temp_lag_1": lag(cpu_temp, 1),
            "cpu_temp_lag_2": lag(cpu_temp, 2),
            "cpu_temp_lag_3": lag(cpu_temp, 3),
            "cpu_temp_delta_1": temp_delta_1,
            "cpu_temp_delta_3": temp_delta_3,
            "cpu_temp_delta_6": temp_delta_6,
            "cpu_temp_delta_12": temp_delta_12,
            "cpu_temp_slope_3": temp_slope_3,
            "cpu_temp_slope_6": temp_slope_6,
            "cpu_temp_slope_12": temp_slope_12,
            "cpu_temp_mean_3": rolling_mean(cpu_temp, 3),
            "cpu_temp_mean_6": rolling_mean(cpu_temp, 6),
            "cpu_temp_mean_12": rolling_mean(cpu_temp, 12),
            "cpu_temp_std_6": rolling_std(cpu_temp, 6),
            "cpu_temp_std_12": rolling_std(cpu_temp, 12),
            "cpu_load_lag_1": lag(cpu_load, 1),
            "cpu_load_delta_1": delta(cpu_load, 1),
            "cpu_load_delta_6": delta(cpu_load, 6),
            "cpu_load_mean_3": rolling_mean(cpu_load, 3),
            "cpu_load_mean_6": rolling_mean(cpu_load, 6),
            "cpu_load_mean_12": rolling_mean(cpu_load, 12),
            "total_power_delta_1": delta(total_power, 1),
            "total_power_delta_6": delta(total_power, 6),
            "total_power_mean_3": rolling_mean(total_power, 3),
            "total_power_mean_12": rolling_mean(total_power, 12),
            "fan_speed_delta_1": delta(fan_mean, 1),
            "fan_speed_delta_6": delta(fan_mean, 6),
            "fan_speed_mean_3": rolling_mean(fan_mean, 3),
            "fan_speed_mean_6": rolling_mean(fan_mean, 6),
            "fan_speed_mean_12": rolling_mean(fan_mean, 12),
            "temp_minus_fan": current_temp - current_fan,
            "power_per_load": power_per_load,
            "expected_temp_without_drift": expected_temp,
            "cooling_deficit": cooling_deficit,
            "estimated_ticks_to_70_slope_3": estimated_ticks_to_threshold(current_temp, temp_slope_3, self.threshold_celsius),
            "estimated_ticks_to_70_slope_6": estimated_ticks_to_threshold(current_temp, temp_slope_6, self.threshold_celsius),
            "estimated_ticks_to_70_slope_12": estimated_ticks_to_threshold(current_temp, temp_slope_12, self.threshold_celsius),
            "fan_high_but_temp_rising": int(current_fan >= 80 and temp_delta_1 > 0),
            "fan_high_temp_still_rising_3": int(current_fan >= 80 and temp_delta_3 > 0),
            "load_high_fan_low": int(current_load >= 80 and current_fan <= 35),
            "cooling_deficit_high": int(cooling_deficit >= 8.0 if not math.isnan(cooling_deficit) else False),
            "temp_rising_fast": int(temp_delta_3 >= 3.0),
            "temp_rising_fast_6": int(temp_delta_6 >= 6.0 if not math.isnan(temp_delta_6) else False),
        }


class OverheatModelAgent:
    def __init__(
        self,
        model_path: Path,
        api_base_url: str,
        threshold_override: float | None,
        low_threshold: float,
        medium_threshold: float,
        high_threshold: float,
        enable_maintenance: bool,
        maintenance_temp_threshold: float,
        maintenance_cooldown_seconds: int,
        dry_run: bool,
    ) -> None:
        bundle = joblib.load(model_path)
        self.model = bundle["model"]
        self.feature_columns: list[str] = bundle["feature_columns"]
        self.model_threshold = float(threshold_override if threshold_override is not None else bundle["threshold"])
        self.low_threshold = low_threshold
        self.medium_threshold = medium_threshold
        self.high_threshold = high_threshold
        self.enable_maintenance = enable_maintenance
        self.maintenance_temp_threshold = maintenance_temp_threshold
        self.maintenance_cooldown_seconds = maintenance_cooldown_seconds
        self.model_name = bundle.get("model_name", type(self.model).__name__)
        self.api_base_url = api_base_url.rstrip("/")
        self.dry_run = dry_run
        self.features = OnlineFeatureBuilder()
        self.fans_by_hostname = self.load_fan_mapping()
        self.last_speed_by_hostname: dict[str, int] = {}
        self.last_action_at: dict[str, float] = {}
        self.last_repair_at_by_fan_id: dict[int, float] = {}

    def load_fan_mapping(self) -> dict[str, list[int]]:
        response = requests.get(f"{self.api_base_url}/fans", timeout=5)
        response.raise_for_status()
        mapping: dict[str, list[int]] = defaultdict(list)
        for fan in response.json():
            hostname = fan.get("server", {}).get("hostname")
            fan_id = fan.get("fan_id")
            if hostname and fan_id is not None:
                mapping[hostname].append(int(fan_id))
        if not mapping:
            raise RuntimeError("No fans returned by API /fans")
        return dict(mapping)

    def sample_from_payload(self, payload: dict[str, Any]) -> Sample | None:
        sensors = {sensor.get("type"): value_or_nan(sensor.get("value")) for sensor in payload.get("sensors", [])}
        hostname = payload.get("hostname")
        if not hostname:
            return None
        return Sample(
            timestamp=str(payload.get("timestamp", "")),
            hostname=hostname,
            cpu_temp=sensors.get("CPU_TEMP", math.nan),
            cpu_load=sensors.get("LOAD", value_or_nan(payload.get("load_percent"))),
            total_power=sensors.get("TOTAL_POWER", math.nan),
            fan_speed_1=sensors.get("FAN_SPEED_1", value_or_nan(payload.get("current_fan_speed"))),
            fan_speed_2=sensors.get("FAN_SPEED_2", value_or_nan(payload.get("current_fan_speed"))),
        )

    def predict_probability(self, hostname: str) -> float:
        feature_map = self.features.features(hostname)
        missing = [column for column in self.feature_columns if column not in feature_map]
        if missing:
            raise RuntimeError(f"Missing online features: {missing}")
        row_values = [[feature_map[column] for column in self.feature_columns]]
        if np.isnan(np.array(row_values, dtype=float)).any():
            raise RuntimeError("Online feature row contains NaN")
        row = pd.DataFrame(row_values, columns=self.feature_columns)
        if hasattr(self.model, "predict_proba"):
            return float(self.model.predict_proba(row)[0, 1])
        return float(self.model.predict(row)[0])

    def target_speed(self, probability: float) -> int:
        if probability >= self.high_threshold:
            return 95
        if probability >= self.medium_threshold:
            return 75
        if probability >= self.low_threshold:
            return 55
        return 35

    def apply_speed(self, hostname: str, speed: int) -> None:
        now = time.time()
        if self.last_speed_by_hostname.get(hostname) == speed and now - self.last_action_at.get(hostname, 0) < 20:
            return

        fan_ids = self.fans_by_hostname.get(hostname, [])
        if not fan_ids:
            print(f"[WARN] No fan mapping for {hostname}")
            return

        skipped_manual: list[int] = []
        try:
            response = requests.get(f"{self.api_base_url}/fans", timeout=5)
            response.raise_for_status()
            fan_state_by_id = {int(fan["fan_id"]): fan for fan in response.json() if fan.get("fan_id") is not None}
            for fan_id in fan_ids:
                fan_state = fan_state_by_id.get(fan_id, {})
                if str(fan_state.get("control_mode", "")).upper() == "MANUAL":
                    skipped_manual.append(fan_id)
        except Exception as exc:
            print(f"[WARN] Could not refresh fan state before PATCH: {exc}")

        if self.dry_run:
            print(f"[DRY-RUN] {hostname}: would set fans {fan_ids} to {speed}%")
        else:
            updated = 0
            for fan_id in fan_ids:
                if fan_id in skipped_manual:
                    continue
                response = requests.patch(
                    f"{self.api_base_url}/fans/{fan_id}",
                    json={"speed_percent": speed},
                    timeout=3,
                )
                if response.status_code >= 400:
                    print(f"[WARN] PATCH /fans/{fan_id} failed: {response.status_code} {response.text}")
                else:
                    updated += 1
            print(f"[ACTION] {hostname}: set {updated}/{len(fan_ids)} fans {fan_ids} to {speed}%")
            if skipped_manual:
                print(f"[ACTION-SKIP] {hostname}: skipped manual/broken fans {skipped_manual}")
                if self.enable_maintenance:
                    self.request_maintenance(hostname, skipped_manual[0], "manual/broken fan detected")

        self.last_speed_by_hostname[hostname] = speed
        self.last_action_at[hostname] = now

    def request_maintenance(self, hostname: str, fan_id: int, reason: str) -> None:
        now = time.time()
        last_repair_at = self.last_repair_at_by_fan_id.get(fan_id, 0)
        if now - last_repair_at < self.maintenance_cooldown_seconds:
            return

        if self.dry_run:
            print(f"[DRY-RUN] {hostname}: would request maintenance for fan {fan_id} ({reason})")
        else:
            response = requests.post(
                f"{self.api_base_url}/sim/maintenance/repair",
                json={"fanId": fan_id},
                timeout=5,
            )
            if response.status_code >= 400:
                print(f"[WARN] POST /sim/maintenance/repair failed for fan {fan_id}: {response.status_code} {response.text}")
            else:
                print(f"[MAINTENANCE] {hostname}: requested repair for fan {fan_id} ({reason}): {response.text}")

        self.last_repair_at_by_fan_id[fan_id] = now

    def maintenance_candidate(self, hostname: str, target_speed: int) -> int | None:
        fan_ids = set(self.fans_by_hostname.get(hostname, []))
        if not fan_ids:
            return None

        response = requests.get(f"{self.api_base_url}/fans", timeout=5)
        response.raise_for_status()

        fallback: int | None = None
        for fan in response.json():
            fan_id = fan.get("fan_id")
            if fan_id is None or int(fan_id) not in fan_ids:
                continue

            fan_id = int(fan_id)
            fallback = fallback or fan_id
            control_mode = str(fan.get("control_mode", "")).upper()
            status = str(fan.get("status", "")).upper()
            speed_percent = value_or_nan(fan.get("speed_percent"))

            if control_mode == "MANUAL" or status != "ON" or speed_percent <= max(5, target_speed - 70):
                return fan_id

        return fallback

    def maybe_request_maintenance(self, sample: Sample, target_speed: int) -> None:
        if not self.enable_maintenance:
            return
        if target_speed < 95:
            return
        if sample.cpu_temp < self.maintenance_temp_threshold:
            return

        feature_map = self.features.features(sample.hostname)
        temp_delta_3 = feature_map.get("cpu_temp_delta_3", math.nan)
        cooling_deficit = feature_map.get("cooling_deficit", math.nan)
        fan_feedback_low = sample.fan_speed_mean <= 60
        cooling_still_bad = cooling_deficit >= 10 if not math.isnan(cooling_deficit) else False
        temp_still_rising = temp_delta_3 > 0 if not math.isnan(temp_delta_3) else False

        if not (fan_feedback_low or cooling_still_bad or temp_still_rising):
            return

        fan_id = self.maintenance_candidate(sample.hostname, target_speed)
        if fan_id is None:
            print(f"[MAINTENANCE-SKIP] {sample.hostname}: no fan candidate")
            return

        self.request_maintenance(sample.hostname, fan_id, "thermal risk despite high fan target")

    def handle_payload(self, payload: dict[str, Any]) -> None:
        sample = self.sample_from_payload(payload)
        if sample is None:
            return
        if any(math.isnan(value) for value in [sample.cpu_temp, sample.cpu_load, sample.total_power, sample.fan_speed_1, sample.fan_speed_2]):
            print(f"[SKIP] {sample.hostname}: incomplete sample")
            return

        self.features.add(sample)
        if not self.features.ready(sample.hostname):
            print(f"[WARMUP] {sample.hostname}: collecting history")
            return

        probability = self.predict_probability(sample.hostname)
        speed = self.target_speed(probability)
        print(
            f"[PRED] {sample.hostname} temp={sample.cpu_temp:.2f} load={sample.cpu_load:.1f} "
            f"fan={sample.fan_speed_mean:.1f} proba={probability:.3f} "
            f"thresholds=({self.low_threshold:.2f},{self.medium_threshold:.2f},{self.high_threshold:.2f}) "
            f"target_fan={speed}%"
        )
        self.maybe_request_maintenance(sample, speed)
        self.apply_speed(sample.hostname, speed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the model-driven MQTT fan agent.")
    parser.add_argument("--model", default="models/overheat_risk_model_h12.joblib", type=Path)
    parser.add_argument("--mqtt-host", default="localhost")
    parser.add_argument("--mqtt-port", default=1883, type=int)
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--api-base-url", default="http://localhost:3333")
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--low-threshold", type=float, default=0.25)
    parser.add_argument("--medium-threshold", type=float, default=0.45)
    parser.add_argument("--high-threshold", type=float, default=0.70)
    parser.add_argument("--disable-maintenance", action="store_true")
    parser.add_argument("--maintenance-temp-threshold", type=float, default=78.0)
    parser.add_argument("--maintenance-cooldown-seconds", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not (0 <= args.low_threshold <= args.medium_threshold <= args.high_threshold <= 1):
        raise SystemExit("Expected 0 <= low-threshold <= medium-threshold <= high-threshold <= 1")

    agent = OverheatModelAgent(
        model_path=args.model,
        api_base_url=args.api_base_url,
        threshold_override=args.threshold,
        low_threshold=args.low_threshold,
        medium_threshold=args.medium_threshold,
        high_threshold=args.high_threshold,
        enable_maintenance=not args.disable_maintenance,
        maintenance_temp_threshold=args.maintenance_temp_threshold,
        maintenance_cooldown_seconds=args.maintenance_cooldown_seconds,
        dry_run=args.dry_run,
    )

    print(f"Loaded model: {agent.model_name}")
    print(f"Model threshold: {agent.model_threshold:.4f}")
    print(
        "Fan policy thresholds: "
        f"low={agent.low_threshold:.2f}->55%, "
        f"medium={agent.medium_threshold:.2f}->75%, "
        f"high={agent.high_threshold:.2f}->95%"
    )
    print(f"Known servers with fans: {len(agent.fans_by_hostname)}")
    print(
        "Maintenance: "
        f"{'enabled' if agent.enable_maintenance else 'disabled'} "
        f"(temp>={agent.maintenance_temp_threshold:.1f}C, cooldown={agent.maintenance_cooldown_seconds}s)"
    )
    print(f"Dry run: {agent.dry_run}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_connect(client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        print(f"Connected to MQTT broker: {reason_code}")
        client.subscribe(args.topic)
        print(f"Subscribed to {args.topic}")

    def on_message(client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            agent.handle_payload(payload)
        except Exception as exc:
            print(f"[ERROR] {exc}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.mqtt_host, args.mqtt_port, 60)
    client.loop_forever()


if __name__ == "__main__":
    main()
