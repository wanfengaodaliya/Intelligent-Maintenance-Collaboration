# Stage 10 Edge H5 Algorithm Physical Layout Design

## Goal

Complete the second independently accepted Stage 10 physical-layout batch by
moving the provider-wrapped bearing H5 algorithm implementation into the
bearing scenario package without changing model behavior, artifacts, runtime
lifecycle, or legacy imports.

This batch covers only the H5 algorithm layer:

- physical-feature extraction and normalization;
- the three-branch H5 network definition;
- distilled H5 artifact loading, input preparation, inference, and evidence
  generation.

The H5 client and lifecycle layer remains a separate later batch.

## Existing Baseline

- The Stage 10 ingestion batch completed with 811 passing tests.
- `BearingEdgeInferenceProvider` and `BearingEdgeModelProvider` already isolate
  the edge service from direct H5 business selection.
- `LocalH5ModelClient` still loads the model through
  `edge_diagnosis.distilled_h5_model`.
- The algorithm implementation currently lives in:
  - `edge_diagnosis/distilled_h5_model.py`;
  - `edge_diagnosis/h5_features.py`;
  - `edge_diagnosis/h5_network.py`.
- Existing verification and runtime code import the old paths, so those paths
  must remain valid.
- The protected worktree list remains:
  - deleted `cloud_edge_project/cloud_service/verification/conftest.py`;
  - deleted `cloud_edge_project/quick_demo.py`;
  - deleted `cloud_edge_project/scheduler/.gitkeep`;
  - untracked `docs/assets/`;
  - untracked `docs/挑战杯项目申报书.docx`.

## Scope

This batch will:

- make `scenarios.bearing.edge_inference.h5` the single implementation owner
  for the three H5 algorithm responsibilities;
- add one explicit bearing V1.2 compatibility export module;
- retain the three old `edge_diagnosis` modules as thin explicit shims;
- preserve existing `edge_diagnosis.__init__` exports;
- add identity, golden-output, import-order, pickle, and architecture tests;
- move one algorithm module at a time and verify it before continuing.

This batch will not:

- move or modify `edge_model/local_h5_client.py`;
- move or modify `edge_model/h5_probe.py`;
- change model-store, activation, rollback, version-pointer, readiness, worker,
  or polling behavior;
- move model weights, normalization data, manifests, probe resources, or Git
  LFS files;
- modify Dockerfile or packaging rules;
- modify the provider API, edge service routes, scheduler, cloud service, or
  other Stage 10 responsibility groups.

## Considered Approaches

### Chosen: dedicated scenario H5 subpackage with legacy compatibility chain

Create `scenarios.bearing.edge_inference.h5`, move the three algorithm files
there, export their retained public names through
`compatibility.bearing_v12.edge_h5_exports`, and retain the old modules as
explicit shims.

This provides a clear algorithm ownership boundary, leaves room for the later
client/probe batch, and preserves one source of truth.

### Rejected: place files directly beside the provider

Moving the files directly under `scenarios.bearing.edge_inference` would use
shorter imports but mix provider assembly, model structure, feature extraction,
and artifact execution in one package. The directory would become unclear when
the later client/probe batch is considered.

### Rejected: scenario aliases over the old implementation

Keeping implementation in `edge_diagnosis` and adding scenario aliases would
minimize immediate changes but would not complete the Stage 10 physical
ownership move.

## Target Structure

```text
cloud_edge_project/
├── scenarios/bearing/edge_inference/
│   ├── __init__.py
│   ├── provider.py
│   └── h5/
│       ├── __init__.py
│       ├── distilled_h5_model.py
│       ├── features.py
│       └── network.py
├── compatibility/bearing_v12/
│   └── edge_h5_exports.py
└── edge_service/src/edge_diagnosis/
    ├── __init__.py
    ├── distilled_h5_model.py
    ├── h5_features.py
    └── h5_network.py
```

The three old modules remain physical files only for import compatibility.

## Dependency Direction

The approved compatibility path is:

```text
LocalH5ModelClient
    -> edge_diagnosis.distilled_h5_model
    -> compatibility.bearing_v12.edge_h5_exports
    -> scenarios.bearing.edge_inference.h5
```

Inside the scenario implementation:

```text
distilled_h5_model
    -> scenarios.bearing.edge_inference.h5.features
    -> scenarios.bearing.edge_inference.h5.network
```

Rules:

- old `edge_diagnosis` modules must not directly import `scenarios.bearing`;
- the compatibility module may import scenario-owned H5 modules;
- scenario-owned H5 modules must not import the three legacy shims;
- generic edge contracts, manifest validation, fallback, model store, and
  version store remain in their existing platform locations;
- compatibility modules and shims must use explicit imports and `__all__`;
- no compatibility file may reimplement feature, network, artifact, inference,
  or evidence behavior.

## Migration Sequence

### Step 1: feature extraction

Move without logic changes:

- `_compute_single`;
- `normalize_features`;
- all existing NumPy/SciPy feature calculations and validation.

Add the compatibility export and replace `edge_diagnosis.h5_features` with a
thin shim. Verify fixed 19-dimensional feature vectors, normalized outputs,
errors, and old/new object identity before Step 2.

### Step 2: network definition

Move without logic changes:

- `PhysicalFusionModel`;
- the existing CNN, physical MLP, condition MLP, dropout, scaling, and fusion
  structure.

Extend the compatibility export and replace `edge_diagnosis.h5_network` with a
thin shim. Verify class identity, state-dict key/shape equality, fixed forward
tensor shapes, and deterministic outputs before Step 3.

### Step 3: distilled model runner

Move without logic changes:

- `H5_LABELS`;
- `RUNTIME_MODEL_VERSION`;
- `H5ModelArtifactError`;
- `DistilledH5DiagnosticModel`;
- its private parsing, normalization, evidence, cancellation, and output
  validation helpers.

Change its two internal imports to scenario-local `features` and `network`,
extend compatibility exports, and replace the old model module with a thin
shim.

### Step 4: complete compatibility and architecture acceptance

Verify `edge_diagnosis.__init__` retains its current public behavior. Confirm
the old modules contain no algorithm definitions and all import orders resolve
without cycles.

## Compatibility Contract

The compatibility export retains these names:

- `H5_LABELS`;
- `RUNTIME_MODEL_VERSION` and the existing package-level
  `H5_RUNTIME_MODEL_VERSION` alias;
- `H5ModelArtifactError`;
- `DistilledH5DiagnosticModel`;
- `_compute_single`;
- `normalize_features`;
- `PhysicalFusionModel`.

Required behavior:

- old and new exceptions, classes, and functions are the same objects;
- constants retain their exact values and required identity;
- existing `from edge_diagnosis.<module> import <name>` statements work;
- `from edge_diagnosis import <name>` retains its current exports;
- old pickle global references resolve through the legacy shims;
- the implementation `__module__` reflects the new scenario location;
- no import path introduces repeated model construction or runtime dispatch
  overhead.

## Frozen Model Behavior

The following must not change:

- 3200-point, 64 kHz vibration input;
- `resample_poly` downsampling by four to 800 points;
- CNN, physical-feature MLP, condition-feature MLP, and fusion topology;
- 19-dimensional physical features and normalization;
- 13-dimensional condition features;
- `cond_scale=0.25`, dropout settings, and other constructor defaults;
- class order: `healthy`, `outer_ring_damage`, `inner_ring_damage`;
- extraction of checkpoint keys prefixed with `h5.`;
- Softmax calculation and six-decimal probability mapping;
- predicted label, confidence, normal/fault result, and risk mapping;
- raw packet validation, evidence fields, statistics, and timestamps;
- cancellation checkpoints and exception behavior;
- artifact manifest validation and readiness metadata;
- model and feature-pipeline version values.

## Artifacts and Resources

Model paths remain caller-supplied. The move does not change:

- `best_model.pt`;
- `physical_feature_normalization.json`;
- `condition_norm.json`;
- model manifests;
- active-version pointers;
- fixed probe NPZ and manifest resources;
- bundled or downloaded model directories;
- Git LFS configuration.

No artifact is copied, renamed, regenerated, or recommitted.

## Error Equivalence

Old and new paths must preserve exact exception type and message for covered
cases including:

- missing or inconsistent model artifacts;
- invalid manifest/version information;
- missing checkpoint `h5.*` weights;
- invalid raw packet structure or identity;
- wrong sample rate, sample count, shape, dtype, or non-finite values;
- invalid normalization arrays;
- invalid probability output;
- inference cancellation.

Any mismatch is a stop condition. Expected errors must not be rewritten to
accommodate the move.

## Verification

### Baseline

- record branch, HEAD, and protected worktree status;
- retain the 811-test full-suite baseline;
- run the existing frozen H5 probe before the first move;
- record input tensors, logits/probabilities, predicted class, confidence,
  risk, result status, and model/version metadata.

### Per-module gates

After each move:

- run the old-path and new-path tests for that module;
- assert retained public object identity;
- compare independent golden outputs and errors;
- test cold imports in both old-first and scenario-first order;
- run `git diff --check`.

The next module may not move until the current gate passes.

### H5 integration

Run at minimum:

- `edge_service/verification/test_distilled_h5_diagnosis.py`;
- `edge_service/verification/test_local_h5_route.py`;
- `edge_service/verification/test_official_model_route.py`;
- `edge_service/verification/test_h5_model_update_lifecycle.py`;
- `edge_service/verification/test_worker_guard.py`;
- `tests/scenarios/bearing/test_edge_inference_provider.py`;
- `scripts/freeze_h5_probe.py` using the existing fixed artifacts.

Acceptance requires:

- exact tensor shape and dtype equality;
- exact class, risk, result, and lifecycle status;
- probabilities within the already established floating-point tolerance;
- unchanged version checks, readiness, activation, rollback, and inference
  failure behavior;
- unchanged weak-network local inference behavior.

### Architecture and full acceptance

- verify one implementation owner for all three files;
- verify the old modules are explicit shims with no algorithm code;
- verify no old shim directly imports the scenario package;
- verify the scenario implementation does not import old H5 shims;
- verify no model weight, resource, client, probe, Docker, or unrelated file
  changed;
- run the complete suite with the formal model environment;
- require at least 811 baseline tests plus new tests;
- request an independent read-only review and fix every Critical or Important
  finding.

## Planned Files

New scenario files:

- `cloud_edge_project/scenarios/bearing/edge_inference/h5/__init__.py`;
- `cloud_edge_project/scenarios/bearing/edge_inference/h5/features.py`;
- `cloud_edge_project/scenarios/bearing/edge_inference/h5/network.py`;
- `cloud_edge_project/scenarios/bearing/edge_inference/h5/distilled_h5_model.py`.

New compatibility file:

- `cloud_edge_project/compatibility/bearing_v12/edge_h5_exports.py`.

Modified legacy files:

- `cloud_edge_project/edge_service/src/edge_diagnosis/h5_features.py`;
- `cloud_edge_project/edge_service/src/edge_diagnosis/h5_network.py`;
- `cloud_edge_project/edge_service/src/edge_diagnosis/distilled_h5_model.py`.

Potentially modified only for explicit compatibility exports:

- `cloud_edge_project/edge_service/src/edge_diagnosis/__init__.py`.

Focused tests and architecture guards will be added or modified. No other
production file is planned for change.

## Stop Conditions

Stop and request direction if:

- fixed-probe tensors, class, risk, action, status, or evidence differ;
- probabilities exceed the existing accepted floating-point tolerance;
- `local_h5_client`, `h5_probe`, model-store, version-store, worker, or poller
  logic must change;
- any model weight, manifest, normalization, probe, Docker, or Git LFS file
  must move or change;
- compatibility requires duplicate implementation;
- another Stage 10 responsibility group must move simultaneously;
- an unrelated user modification overlaps a target file.

## Success Criteria

- The three H5 algorithm implementations exist only under
  `scenarios.bearing.edge_inference.h5`.
- Legacy `edge_diagnosis` imports remain valid through the V1.2 compatibility
  boundary.
- Public objects, model structure, features, tensors, outputs, evidence,
  errors, and versions remain equivalent.
- Client, probe, artifacts, weights, resources, Docker, and lifecycle behavior
  remain unchanged.
- Focused and full tests pass.
- Independent review reports no remaining Critical or Important finding.
- This H5 algorithm batch is reported complete without automatically starting
  the H5 client/probe batch.
