#!/usr/bin/env python3
"""Train baseline models for near-future overheating prediction.

This script expects the CSV produced by `ml/prepare_overheat_dataset.py`.
It trains several simple models, tunes a probability threshold on validation
data, prints comparable metrics, and saves the best bundle with joblib.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import ExtraTreesClassifier, GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
)
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split


NON_FEATURE_COLUMNS = {
    "run_id",
    "time",
    "server_id",
    "hostname",
}


def find_label_column(df: pd.DataFrame) -> str:
    matches = [column for column in df.columns if column.startswith("risk_overheat_next_")]
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one risk_overheat label column, found: {matches}")
    return matches[0]


def temporal_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split by time as a fallback until multiple run_ids are available."""
    df = df.sort_values(["time", "server_id"]).reset_index(drop=True)
    train_cut = int(len(df) * 0.70)
    valid_cut = int(len(df) * 0.85)
    return df.iloc[:train_cut].copy(), df.iloc[train_cut:valid_cut].copy(), df.iloc[valid_cut:].copy()


def run_id_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Prefer splitting by run_id when several independent simulations exist."""
    run_ids = sorted(df["run_id"].unique())
    if len(run_ids) < 3:
        return temporal_split(df)

    train_count = max(1, int(len(run_ids) * 0.70))
    valid_count = max(1, int(len(run_ids) * 0.15))
    train_ids = set(run_ids[:train_count])
    valid_ids = set(run_ids[train_count : train_count + valid_count])
    test_ids = set(run_ids[train_count + valid_count :])
    if not test_ids:
        test_ids = {run_ids[-1]}
        train_ids.discard(run_ids[-1])

    return (
        df[df["run_id"].isin(train_ids)].copy(),
        df[df["run_id"].isin(valid_ids)].copy(),
        df[df["run_id"].isin(test_ids)].copy(),
    )


def stratified_split(df: pd.DataFrame, label_col: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Use only as a smoke test when there is one historical export.

    This is weaker than a temporal or run_id split because nearby rows from the
    same incident can land in train and test. It proves the pipeline runs, not
    that the model generalizes to a new scenario.
    """
    train_df, temp_df = train_test_split(
        df,
        test_size=0.30,
        random_state=42,
        stratify=df[label_col],
    )
    valid_df, test_df = train_test_split(
        temp_df,
        test_size=0.50,
        random_state=42,
        stratify=temp_df[label_col],
    )
    return train_df.copy(), valid_df.copy(), test_df.copy()


def validate_split(name: str, split_df: pd.DataFrame, label_col: str) -> None:
    counts = split_df[label_col].value_counts().to_dict()
    if split_df[label_col].nunique() < 2:
        raise SystemExit(
            f"{name} split has one class only: {counts}. This dataset cannot train/evaluate "
            "a reliable classifier with the selected split. Generate more incident runs, "
            "or use --split stratified only for a smoke test."
        )


def best_threshold(y_true: pd.Series, probabilities: np.ndarray) -> float:
    precision, recall, thresholds = precision_recall_curve(y_true, probabilities)
    if len(thresholds) == 0:
        return 0.5
    f1 = (2 * precision[:-1] * recall[:-1]) / (precision[:-1] + recall[:-1] + 1e-12)
    return float(thresholds[int(np.nanargmax(f1))])


def candidate_models() -> dict[str, object]:
    return {
        "dummy": DummyClassifier(strategy="most_frequent"),
        "logistic_regression": make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=3000, class_weight="balanced"),
        ),
        "random_forest": RandomForestClassifier(
            n_estimators=400,
            max_depth=10,
            min_samples_leaf=3,
            class_weight="balanced_subsample",
            random_state=42,
            n_jobs=-1,
        ),
        "extra_trees": ExtraTreesClassifier(
            n_estimators=400,
            max_depth=10,
            min_samples_leaf=3,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
        ),
        "gradient_boosting": GradientBoostingClassifier(random_state=42),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train overheat prediction baselines.")
    parser.add_argument("--input", default="data/training_dataset_overheat.csv", type=Path)
    parser.add_argument("--output", default="models/overheat_risk_model.joblib", type=Path)
    parser.add_argument(
        "--split",
        choices=["auto", "temporal", "stratified"],
        default="auto",
        help=(
            "auto uses run_id split when possible, otherwise temporal. "
            "stratified is for pipeline smoke tests only."
        ),
    )
    args = parser.parse_args()

    df = pd.read_csv(args.input, parse_dates=["time"])
    label_col = find_label_column(df)
    target_counts = df[label_col].value_counts().to_dict()
    if len(target_counts) < 2:
        raise SystemExit(
            f"Cannot train a classifier: label column {label_col} has one class only: "
            f"{target_counts}. Generate hotter scenarios or lower the threshold for a sanity check."
        )

    feature_cols = [
        column
        for column in df.columns
        if column not in NON_FEATURE_COLUMNS and column != label_col
    ]
    df[feature_cols] = df[feature_cols].apply(pd.to_numeric, errors="coerce")
    df = df.dropna(subset=feature_cols + [label_col]).reset_index(drop=True)

    if args.split == "stratified":
        train_df, valid_df, test_df = stratified_split(df, label_col)
    elif args.split == "temporal":
        train_df, valid_df, test_df = temporal_split(df)
    else:
        train_df, valid_df, test_df = run_id_split(df)

    if valid_df.empty or test_df.empty:
        raise SystemExit("Validation or test split is empty. Add more rows or more run_ids.")

    validate_split("Train", train_df, label_col)
    validate_split("Validation", valid_df, label_col)
    validate_split("Test", test_df, label_col)

    x_train, y_train = train_df[feature_cols], train_df[label_col].astype(int)
    x_valid, y_valid = valid_df[feature_cols], valid_df[label_col].astype(int)
    x_test, y_test = test_df[feature_cols], test_df[label_col].astype(int)

    print("Dataset:")
    print(f"  rows={len(df)} features={len(feature_cols)} label={label_col}")
    print(f"  split={args.split} train={len(train_df)} valid={len(valid_df)} test={len(test_df)}")
    print(f"  label distribution={df[label_col].value_counts(normalize=True).to_dict()}")
    print(f"  train labels={train_df[label_col].value_counts().to_dict()}")
    print(f"  valid labels={valid_df[label_col].value_counts().to_dict()}")
    print(f"  test labels={test_df[label_col].value_counts().to_dict()}")

    rows = []
    trained: dict[str, object] = {}
    thresholds: dict[str, float] = {}

    for name, model in candidate_models().items():
        model.fit(x_train, y_train)
        trained[name] = model

        if hasattr(model, "predict_proba") and y_valid.nunique() == 2:
            valid_proba = model.predict_proba(x_valid)[:, 1]
            threshold = best_threshold(y_valid, valid_proba)
            test_proba = model.predict_proba(x_test)[:, 1]
            test_pred = (test_proba >= threshold).astype(int)
            avg_precision = average_precision_score(y_test, test_proba)
        else:
            threshold = 0.5
            test_pred = model.predict(x_test)
            avg_precision = np.nan

        thresholds[name] = threshold
        rows.append(
            {
                "model": name,
                "threshold": threshold,
                "f1": f1_score(y_test, test_pred, zero_division=0),
                "average_precision": avg_precision,
            }
        )

    results = pd.DataFrame(rows).sort_values(["f1", "average_precision"], ascending=False)
    print("\nModel comparison:")
    print(results.to_string(index=False))

    best_name = str(results.iloc[0]["model"])
    best_model = trained[best_name]
    best_cutoff = thresholds[best_name]
    if hasattr(best_model, "predict_proba"):
        y_pred = (best_model.predict_proba(x_test)[:, 1] >= best_cutoff).astype(int)
    else:
        y_pred = best_model.predict(x_test)

    print(f"\nBest model: {best_name} at threshold={best_cutoff:.4f}")
    print(classification_report(y_test, y_pred, zero_division=0))
    print("Confusion matrix [[TN, FP], [FN, TP]]:")
    print(confusion_matrix(y_test, y_pred))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": best_model,
            "model_name": best_name,
            "threshold": best_cutoff,
            "feature_columns": feature_cols,
            "label_column": label_col,
            "results": results,
        },
        args.output,
        compress=3,
    )
    print(f"\nSaved model bundle: {args.output}")


if __name__ == "__main__":
    main()
