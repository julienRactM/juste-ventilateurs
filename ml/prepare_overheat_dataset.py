#!/usr/bin/env python3
"""Build a supervised dataset for near-future overheating prediction.

The raw input is expected to contain one row per timestamp and server, with at
least these columns:

    time, server_id, hostname, cpu_temp, cpu_load, total_power,
    fan_speed_1, fan_speed_2

The generated label is:

    risk_overheat_next_N_ticks = 1

when the same server reaches or exceeds the configured temperature threshold in
the next N ticks. This script intentionally uses only the Python standard
library so it can run before the ML dependencies are installed.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Iterable


DEFAULT_SENSOR_COLUMNS = (
    "cpu_temp",
    "cpu_load",
    "total_power",
    "fan_speed_1",
    "fan_speed_2",
)


@dataclass
class DatasetStats:
    rows: int = 0
    positives: int = 0
    servers: int = 0
    min_temp: float | None = None
    max_temp: float | None = None

    @property
    def positive_rate(self) -> float:
        return self.positives / self.rows if self.rows else 0.0


def parse_time(value: str) -> datetime:
    """Parse the timestamp formats produced by the current simulator exports."""
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def to_float(row: dict[str, str], column: str) -> float:
    value = row.get(column)
    if value is None or value == "":
        return math.nan
    return float(value)


def rolling_mean(values: list[float], index: int, window: int) -> float:
    start = max(0, index - window + 1)
    usable = [v for v in values[start : index + 1] if not math.isnan(v)]
    return mean(usable) if usable else math.nan


def rolling_std(values: list[float], index: int, window: int) -> float:
    start = max(0, index - window + 1)
    usable = [v for v in values[start : index + 1] if not math.isnan(v)]
    return pstdev(usable) if len(usable) >= 2 else 0.0


def slope(values: list[float], index: int, window: int) -> float:
    """Return the per-tick slope between now and `window` ticks ago."""
    previous = lag(values, index, window)
    current = values[index]
    if math.isnan(previous) or math.isnan(current):
        return math.nan
    return (current - previous) / window


def lag(values: list[float], index: int, steps: int) -> float:
    target = index - steps
    return values[target] if target >= 0 else math.nan


def delta(values: list[float], index: int, steps: int) -> float:
    previous = lag(values, index, steps)
    current = values[index]
    if math.isnan(previous) or math.isnan(current):
        return math.nan
    return current - previous


def current_or_future_overheat_label(
    cpu_temps: list[float],
    index: int,
    horizon_ticks: int,
    threshold_celsius: float,
) -> int:
    current_and_future = cpu_temps[index : index + horizon_ticks + 1]
    return int(any(temp >= threshold_celsius for temp in current_and_future if not math.isnan(temp)))


def estimated_ticks_to_threshold(current_temp: float, temp_slope: float, threshold_celsius: float) -> float:
    """Estimate time to threshold from current trend.

    A capped value keeps the feature numerical when the temperature is stable or
    cooling. The model only needs to distinguish "soon" from "not soon".
    """
    if math.isnan(current_temp) or math.isnan(temp_slope) or temp_slope <= 0:
        return 999.0
    if current_temp >= threshold_celsius:
        return 0.0
    return min(999.0, (threshold_celsius - current_temp) / temp_slope)


def expected_temp_without_drift(cpu_load: float, fan_speed: float) -> float:
    """Approximate the simulator thermal equation without scenario drift.

    The current SimulationService uses an ambient temperature of 22C for
    persisted sensor_data. This feature estimates whether the observed
    temperature is hotter than load and fan speed alone should explain.
    """
    if math.isnan(cpu_load) or math.isnan(fan_speed):
        return math.nan
    load_ratio = max(0.0, min(1.0, cpu_load / 100.0))
    temp = 22.0 + (load_ratio * 55.0)
    if fan_speed > 0:
        temp -= (fan_speed / 100.0) * 25.0 * (load_ratio + 0.3)
    return max(22.0, min(105.0, temp))


def load_rows(input_path: Path) -> list[dict[str, str]]:
    with input_path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        required = {"time", "server_id", "hostname", *DEFAULT_SENSOR_COLUMNS}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"Missing columns in {input_path}: {sorted(missing)}")
        return list(reader)


def format_float(value: float) -> str:
    if math.isnan(value) or math.isinf(value):
        return ""
    return f"{value:.6f}"


def build_dataset(
    rows: Iterable[dict[str, str]],
    threshold_celsius: float,
    horizon_ticks: int,
    run_id: str,
) -> tuple[list[dict[str, str]], DatasetStats]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        row = dict(row)
        row["_parsed_time"] = parse_time(row["time"])
        grouped[row["server_id"]].append(row)

    output_rows: list[dict[str, str]] = []
    stats = DatasetStats(servers=len(grouped))

    for server_id, server_rows in grouped.items():
        server_rows.sort(key=lambda item: item["_parsed_time"])
        series = {
            column: [to_float(row, column) for row in server_rows]
            for column in DEFAULT_SENSOR_COLUMNS
        }
        fan_mean = [
            mean([series["fan_speed_1"][idx], series["fan_speed_2"][idx]])
            for idx in range(len(server_rows))
        ]

        for idx, row in enumerate(server_rows):
            cpu_temp = series["cpu_temp"][idx]
            cpu_load = series["cpu_load"][idx]
            total_power = series["total_power"][idx]
            current_fan_mean = fan_mean[idx]

            # We need enough history for meaningful trend features and enough
            # future rows for a clean label.
            if idx < 3 or idx + horizon_ticks >= len(server_rows):
                continue

            label = current_or_future_overheat_label(
                series["cpu_temp"],
                idx,
                horizon_ticks=horizon_ticks,
                threshold_celsius=threshold_celsius,
            )

            temp_delta_1 = delta(series["cpu_temp"], idx, 1)
            temp_delta_3 = delta(series["cpu_temp"], idx, 3)
            temp_delta_6 = delta(series["cpu_temp"], idx, 6)
            temp_delta_12 = delta(series["cpu_temp"], idx, 12)
            temp_slope_3 = slope(series["cpu_temp"], idx, 3)
            temp_slope_6 = slope(series["cpu_temp"], idx, 6)
            temp_slope_12 = slope(series["cpu_temp"], idx, 12)
            fan_delta_1 = fan_mean[idx] - fan_mean[idx - 1]
            fan_delta_6 = fan_mean[idx] - fan_mean[idx - 6] if idx >= 6 else math.nan
            power_per_load = total_power / cpu_load if cpu_load > 0 else math.nan
            temp_minus_fan = cpu_temp - current_fan_mean
            expected_temp = expected_temp_without_drift(cpu_load, current_fan_mean)
            cooling_deficit = cpu_temp - expected_temp if not math.isnan(expected_temp) else math.nan
            estimated_ticks_3 = estimated_ticks_to_threshold(cpu_temp, temp_slope_3, threshold_celsius)
            estimated_ticks_6 = estimated_ticks_to_threshold(cpu_temp, temp_slope_6, threshold_celsius)
            estimated_ticks_12 = estimated_ticks_to_threshold(cpu_temp, temp_slope_12, threshold_celsius)

            output = {
                "run_id": run_id,
                "time": row["time"],
                "server_id": server_id,
                "hostname": row["hostname"],
                "cpu_temp": format_float(cpu_temp),
                "cpu_load": format_float(cpu_load),
                "total_power": format_float(total_power),
                "fan_speed_1": format_float(series["fan_speed_1"][idx]),
                "fan_speed_2": format_float(series["fan_speed_2"][idx]),
                "fan_speed_mean": format_float(current_fan_mean),
                "cpu_temp_lag_1": format_float(lag(series["cpu_temp"], idx, 1)),
                "cpu_temp_lag_2": format_float(lag(series["cpu_temp"], idx, 2)),
                "cpu_temp_lag_3": format_float(lag(series["cpu_temp"], idx, 3)),
                "cpu_temp_delta_1": format_float(temp_delta_1),
                "cpu_temp_delta_3": format_float(temp_delta_3),
                "cpu_temp_delta_6": format_float(temp_delta_6),
                "cpu_temp_delta_12": format_float(temp_delta_12),
                "cpu_temp_slope_3": format_float(temp_slope_3),
                "cpu_temp_slope_6": format_float(temp_slope_6),
                "cpu_temp_slope_12": format_float(temp_slope_12),
                "cpu_temp_mean_3": format_float(rolling_mean(series["cpu_temp"], idx, 3)),
                "cpu_temp_mean_6": format_float(rolling_mean(series["cpu_temp"], idx, 6)),
                "cpu_temp_mean_12": format_float(rolling_mean(series["cpu_temp"], idx, 12)),
                "cpu_temp_std_6": format_float(rolling_std(series["cpu_temp"], idx, 6)),
                "cpu_temp_std_12": format_float(rolling_std(series["cpu_temp"], idx, 12)),
                "cpu_load_lag_1": format_float(lag(series["cpu_load"], idx, 1)),
                "cpu_load_delta_1": format_float(delta(series["cpu_load"], idx, 1)),
                "cpu_load_delta_6": format_float(delta(series["cpu_load"], idx, 6)),
                "cpu_load_mean_3": format_float(rolling_mean(series["cpu_load"], idx, 3)),
                "cpu_load_mean_6": format_float(rolling_mean(series["cpu_load"], idx, 6)),
                "cpu_load_mean_12": format_float(rolling_mean(series["cpu_load"], idx, 12)),
                "total_power_delta_1": format_float(delta(series["total_power"], idx, 1)),
                "total_power_delta_6": format_float(delta(series["total_power"], idx, 6)),
                "total_power_mean_3": format_float(rolling_mean(series["total_power"], idx, 3)),
                "total_power_mean_12": format_float(rolling_mean(series["total_power"], idx, 12)),
                "fan_speed_delta_1": format_float(fan_delta_1),
                "fan_speed_delta_6": format_float(fan_delta_6),
                "fan_speed_mean_3": format_float(rolling_mean(fan_mean, idx, 3)),
                "fan_speed_mean_6": format_float(rolling_mean(fan_mean, idx, 6)),
                "fan_speed_mean_12": format_float(rolling_mean(fan_mean, idx, 12)),
                "temp_minus_fan": format_float(temp_minus_fan),
                "power_per_load": format_float(power_per_load),
                "expected_temp_without_drift": format_float(expected_temp),
                "cooling_deficit": format_float(cooling_deficit),
                "estimated_ticks_to_70_slope_3": format_float(estimated_ticks_3),
                "estimated_ticks_to_70_slope_6": format_float(estimated_ticks_6),
                "estimated_ticks_to_70_slope_12": format_float(estimated_ticks_12),
                "fan_high_but_temp_rising": str(int(current_fan_mean >= 80 and temp_delta_1 > 0)),
                "fan_high_temp_still_rising_3": str(int(current_fan_mean >= 80 and temp_delta_3 > 0)),
                "load_high_fan_low": str(int(cpu_load >= 80 and current_fan_mean <= 35)),
                "cooling_deficit_high": str(int(cooling_deficit >= 8.0 if not math.isnan(cooling_deficit) else False)),
                "temp_rising_fast": str(int(temp_delta_3 >= 3.0)),
                "temp_rising_fast_6": str(int(temp_delta_6 >= 6.0 if not math.isnan(temp_delta_6) else False)),
                f"risk_overheat_next_{horizon_ticks}_ticks": str(label),
            }
            output_rows.append(output)

            stats.rows += 1
            stats.positives += label
            stats.min_temp = cpu_temp if stats.min_temp is None else min(stats.min_temp, cpu_temp)
            stats.max_temp = cpu_temp if stats.max_temp is None else max(stats.max_temp, cpu_temp)

    return output_rows, stats


def write_dataset(output_path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        raise SystemExit("No training rows generated. Check input data and horizon.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare a supervised overheat prediction dataset."
    )
    parser.add_argument("--input", default="dataset_ml.csv", type=Path)
    parser.add_argument("--output", default="data/training_dataset_overheat.csv", type=Path)
    parser.add_argument("--run-id", default="historical_export")
    parser.add_argument("--threshold", default=70.0, type=float)
    parser.add_argument("--horizon", default=4, type=int)
    args = parser.parse_args()

    raw_rows = load_rows(args.input)
    dataset_rows, stats = build_dataset(
        raw_rows,
        threshold_celsius=args.threshold,
        horizon_ticks=args.horizon,
        run_id=args.run_id,
    )
    write_dataset(args.output, dataset_rows)

    print(f"Prepared dataset: {args.output}")
    print(f"Rows: {stats.rows}")
    print(f"Servers: {stats.servers}")
    print(f"Temperature range: {stats.min_temp:.2f}C -> {stats.max_temp:.2f}C")
    print(f"Positive labels: {stats.positives} ({stats.positive_rate:.2%})")
    if stats.positives == 0:
        print(
            "WARNING: no positive labels were generated. Lower --threshold for a "
            "sanity-check model, or generate hotter incident scenarios before "
            "training the real model."
        )
    elif stats.positive_rate < 0.01:
        print(
            "WARNING: positive labels are very rare. Train/test metrics may be "
            "unstable until more incident scenarios are generated."
        )


if __name__ == "__main__":
    main()
