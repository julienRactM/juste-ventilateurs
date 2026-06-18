# Overheat Risk Model Pipeline

This is the first clean ML target for the project:

```text
risk_overheat_next_4_ticks
```

It predicts whether a server will cross a temperature threshold in the next 4
simulation ticks.

## Why This Target First

The current model is not a strong basis for real prediction because several
labels are synthetic or derived from scenario timing. This target is easier to
validate: the future temperature either crosses the threshold or it does not.

## Step 1: Prepare The Dataset

Run:

```bash
python3 ml/prepare_overheat_dataset.py \
  --input dataset_ml.csv \
  --output data/training_dataset_overheat.csv \
  --threshold 70 \
  --horizon 4
```

Important: the current `dataset_ml.csv` has a maximum observed CPU temperature
around 61.7C. With `--threshold 70`, it will probably produce zero positive
labels. That is expected and means the current data is not hot enough for the
real target.

For a pipeline sanity check only, use a lower threshold:

```bash
python3 ml/prepare_overheat_dataset.py --threshold 60
```

Do not present the `60C` model as the final model. It only proves that the
training code runs end to end.

## Step 2: Train Baseline Models

Install dependencies first:

```bash
python3 -m pip install -r requirements.txt
```

Then:

```bash
python3 ml/train_overheat_model.py \
  --input data/training_dataset_overheat.csv \
  --output models/overheat_risk_model.joblib
```

The default split is intentionally strict. With the current single historical
export, it may stop with a message such as:

```text
Validation split has one class only
```

That is a useful failure: it means the dataset does not contain enough incident
examples in the right places to train and evaluate a real predictive model.

For a technical smoke test only, run:

```bash
python3 ml/train_overheat_model.py \
  --input data/training_dataset_overheat_threshold55.csv \
  --output models/overheat_risk_model_threshold55_smoke.joblib \
  --split stratified
```

Do not use the smoke-test score as proof that the model is good. Rows from the
same incident can be mixed across train and test.

The script compares:

- dummy classifier
- logistic regression
- random forest
- extra trees
- gradient boosting

It saves the best model bundle with:

- model object
- selected probability threshold
- feature columns
- label column
- comparison table

## Step 3: Generate Better Training Data

The final model needs several independent simulation runs, not just one
Marseille export.

Minimum recommended runs:

- healthy week
- load spike only
- fan crash only
- thermal drift only
- load spike plus fan crash
- high load but sufficient cooling
- high temperature without fan failure
- progressive fan degradation
- Marseille crisis kept for final test

Each run should have a different `run_id`. The training script automatically
prefers splitting by `run_id` when several runs exist. This is stronger than a
random row split because it tests generalization to unseen simulations.

## Step 4: What Good Looks Like

Do not optimize for accuracy. Use:

- recall on positive overheating cases
- precision of alerts
- F1
- average precision
- confusion matrix
- detection lead time before the threshold crossing

The model is useful only if it beats simple baselines:

- fans fixed at 100%
- fans fixed at 50%
- rule: `if temp > 60 then fan = 100`
- proportional controller without ML
