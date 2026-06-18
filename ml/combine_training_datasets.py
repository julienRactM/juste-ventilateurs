#!/usr/bin/env python3
"""Combine prepared training datasets while keeping a single CSV header."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Combine prepared ML CSV files.")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)

    header: list[str] | None = None
    total_rows = 0
    run_ids: set[str] = set()

    with args.output.open("w", newline="", encoding="utf-8") as out_file:
        writer: csv.DictWriter[str] | None = None

        for input_path in args.inputs:
            with input_path.open(newline="", encoding="utf-8") as in_file:
                reader = csv.DictReader(in_file)
                if reader.fieldnames is None:
                    raise SystemExit(f"No header found in {input_path}")

                if header is None:
                    header = list(reader.fieldnames)
                    writer = csv.DictWriter(out_file, fieldnames=header)
                    writer.writeheader()
                elif list(reader.fieldnames) != header:
                    raise SystemExit(f"Header mismatch in {input_path}")

                assert writer is not None
                rows_in_file = 0
                for row in reader:
                    writer.writerow(row)
                    rows_in_file += 1
                    total_rows += 1
                    if row.get("run_id"):
                        run_ids.add(row["run_id"])

                print(f"{input_path}: {rows_in_file} rows")

    print(f"Combined dataset: {args.output}")
    print(f"Total rows: {total_rows}")
    print(f"Run ids: {len(run_ids)}")
    for run_id in sorted(run_ids):
        print(f"  - {run_id}")


if __name__ == "__main__":
    main()
