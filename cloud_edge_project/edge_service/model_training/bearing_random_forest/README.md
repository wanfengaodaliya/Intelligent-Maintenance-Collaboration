# Bearing random-forest training

This directory contains the reproducible training pipeline for the packet-level
bearing fault classifier used by `edge_service`.

The model is binary:

- `healthy` -> `normal`
- `outer_ring_damage` or `inner_ring_damage` -> `fault`

It consumes the 27 numeric fields defined in `schema/feature_schema.json`. The
runtime model does not distinguish inner-race and outer-race faults and does not
produce a `warning` class.

## Data

The Paderborn `.mat` files and generated Parquet/JSONL features are deliberately
not stored in Git. Download the Paderborn Bearing DataCenter data separately and
arrange it according to `manifests/dataset_split.csv`. The source dataset must be
supplied through `--dataset-root`; generated files should be written outside the
repository or to an ignored local directory.

## Environment

From the repository root:

```powershell
py -3.10 -m venv .venv-rf
.\.venv-rf\Scripts\python.exe -m pip install -r `
  .\cloud_edge_project\edge_service\model_training\bearing_random_forest\requirements-rf.txt
```

The extractor reuses `edge_service` perception and sender modules, so run it as
a module from the repository root:

```powershell
$python = ".\.venv-rf\Scripts\python.exe"
$package = "cloud_edge_project.edge_service.model_training.bearing_random_forest"
$dataset = "D:\path\to\paderborn-layout"
$run = "D:\bearing-rf-output\features"
$model = ".\cloud_edge_project\edge_service\models\bearing_random_forest"

& $python -m "$package.extract" --dataset-root $dataset --output-dir $run --workers 6
& $python -m "$package.audit" --run-dir $run --require-complete
& $python -m "$package.train" cv `
  --features "$run\features.parquet" `
  --model-dir $model `
  --dataset-root $dataset `
  --experiments ".\cloud_edge_project\edge_service\model_training\bearing_random_forest\experiments.json"
```

## Evaluation-only runtime export

The committed A2 model is a real fitted random forest, but its bearing-isolated
cross-validation gate did not pass. It is therefore marked `evaluation_only` and
must not be described as production-validated. The explicit exporter preserves
that status while producing a loadable runtime artifact:

```powershell
& $python -m "$package.export_evaluation_model" `
  --features "$run\features.parquet" `
  --output-dir $model
```

This command never changes the recorded cross-validation result and never marks
the model deployable.
