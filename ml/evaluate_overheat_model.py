#!/usr/bin/env python3
"""Evaluate a saved overheat model bundle on a prepared dataset."""

from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import pandas as pd
from sklearn.metrics import average_precision_score, classification_report, confusion_matrix, precision_recall_curve


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a saved overheat model.")
    parser.add_argument("--model", default="models/overheat_risk_model.joblib", type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Override the saved model threshold for evaluation.",
    )
    parser.add_argument(
        "--show-thresholds",
        action="store_true",
        help="Print candidate probability thresholds from the precision/recall curve.",
    )
    parser.add_argument(
        "--rule-temp-threshold",
        type=float,
        default=None,
        help="Optional safety override: predict positive when cpu_temp is at or above this value.",
    )
    args = parser.parse_args()

    bundle = joblib.load(args.model)
    model = bundle["model"]
    threshold = float(args.threshold if args.threshold is not None else bundle["threshold"])
    feature_columns = bundle["feature_columns"]
    label_column = bundle["label_column"]

    df = pd.read_csv(args.input, parse_dates=["time"])
    missing = set(feature_columns + [label_column]) - set(df.columns)
    if missing:
        raise SystemExit(f"Missing columns in evaluation dataset: {sorted(missing)}")

    df[feature_columns] = df[feature_columns].apply(pd.to_numeric, errors="coerce")
    df = df.dropna(subset=feature_columns + [label_column]).reset_index(drop=True)

    y_true = df[label_column].astype(int)
    if y_true.nunique() < 2:
        print(f"WARNING: evaluation dataset has one class only: {y_true.value_counts().to_dict()}")

    if hasattr(model, "predict_proba"):
        probabilities = model.predict_proba(df[feature_columns])[:, 1]
        y_pred = (probabilities >= threshold).astype(int)
        avg_precision = average_precision_score(y_true, probabilities) if y_true.nunique() == 2 else None
    else:
        probabilities = None
        y_pred = model.predict(df[feature_columns])
        avg_precision = None

    if args.rule_temp_threshold is not None:
        rule_pred = (pd.to_numeric(df["cpu_temp"], errors="coerce") >= args.rule_temp_threshold).astype(int)
        y_pred = ((y_pred == 1) | (rule_pred == 1)).astype(int)
        print(f"Safety override enabled: cpu_temp >= {args.rule_temp_threshold}")

    print(f"Model: {bundle.get('model_name', type(model).__name__)}")
    print(f"Threshold: {threshold:.4f}")
    if args.threshold is not None:
        print(f"Saved threshold: {float(bundle['threshold']):.4f}")
    print(f"Rows: {len(df)}")
    print(f"Label distribution: {y_true.value_counts().to_dict()}")
    if avg_precision is not None:
        print(f"Average precision: {avg_precision:.6f}")
    print()
    print(classification_report(y_true, y_pred, zero_division=0))
    print("Confusion matrix [[TN, FP], [FN, TP]]:")
    print(confusion_matrix(y_true, y_pred))

    if args.show_thresholds and probabilities is not None and y_true.nunique() == 2:
        precision, recall, thresholds = precision_recall_curve(y_true, probabilities)
        candidates = []
        for idx, cutoff in enumerate(thresholds):
            if idx >= len(precision) - 1:
                continue
            f1 = (2 * precision[idx] * recall[idx]) / (precision[idx] + recall[idx] + 1e-12)
            candidates.append((float(cutoff), float(precision[idx]), float(recall[idx]), float(f1)))

        print()
        print("Candidate thresholds sorted by F1:")
        for cutoff, prec, rec, f1 in sorted(candidates, key=lambda item: item[3], reverse=True)[:15]:
            print(f"threshold={cutoff:.6f} precision={prec:.3f} recall={rec:.3f} f1={f1:.3f}")

    if probabilities is not None:
        out = df[["run_id", "time", "server_id", "hostname", "cpu_temp", label_column]].copy()
        out["pred_probability"] = probabilities
        out["pred_label"] = y_pred
        errors = out[out[label_column].astype(int) != out["pred_label"]]
        print()
        print("Top prediction errors:")
        if errors.empty:
            print("None")
        else:
            print(errors.sort_values("pred_probability", ascending=False).head(20).to_string(index=False))


if __name__ == "__main__":
    main()
