# Stage 10 Cloud MOMENT Runtime Physical Layout Design

## Goal

Complete one independently accepted Stage 10 physical-layout batch by moving
the already provider-wrapped bearing MOMENT diagnosis runtime out of the
generic cloud service package and into the bearing scenario package. Preserve
all existing cloud imports, model loading, prediction, lifecycle, response,
error, and fallback behavior.

This batch covers only:

- the MOMENT-1-small backbone loader;
- the LIGHT_ADAPT runner and prediction value object;
- the 13-dimensional bearing operating-condition transform;
- the fixed bearing label order and review decision mapping;
- the deployment-workspace lookup used to load the saved model definition.

Offline training, model assets, model lifecycle orchestration, cloud request
orchestration, global analysis, storage SQL, and API behavior are separate
responsibilities and are out of scope.

## Existing Baseline

- Branch: `challenge-cup-application`.
- Accepted Stage 10 H5 batch HEAD: `9e080e2`.
- Full-suite baseline: 855 passing tests.
- `BearingCloudDiagnosisProvider` already exposes the verified bearing cloud
  handler through the scenario registry.
- `cloud_service.service` still imports the MOMENT runner and review policy
  through `cloud_service.moment_light_adapt`.
- `cloud_service.model_update.training` imports the backbone loader through
  `cloud_service.moment_backbone`.
- Those two legacy modules still own bearing-specific labels, condition
  fields, decision mappings, model loading, and inference behavior.
- The protected worktree entries remain:
  - deleted `cloud_edge_project/cloud_service/verification/conftest.py`;
  - deleted `cloud_edge_project/quick_demo.py`;
  - deleted `cloud_edge_project/scheduler/.gitkeep`;
  - untracked `docs/assets/`;
  - untracked `docs/挑战杯项目申报书.docx`.

## Considered Approaches

### Chosen: scenario implementation with two compatibility boundaries

Move both implementations into `scenarios.bearing.cloud_diagnosis`, export
their retained public names through one explicit V1.2 compatibility module,
and keep both old cloud-service modules as explicit re-export shims.

This provides one implementation owner, preserves old imports and object
identity, and avoids a forbidden direct import from generic cloud code to the
bearing scenario.

### Rejected: scenario aliases over cloud-service implementation

This would have the smallest diff, but `cloud_service` would remain the owner
of bearing labels, condition fields, decision rules, and model implementation.
It therefore would not complete the Stage 10 physical ownership change.

### Rejected: simultaneous provider injection into cloud orchestration

Changing `cloud_service.service` to accept a scenario runtime provider could
further reduce compatibility imports, but it would mix physical movement with
cloud inference and model-lifecycle control-flow changes. That expansion is
unnecessary for this batch and increases behavior risk.

## Target Structure

```text
cloud_edge_project/
├── scenarios/bearing/cloud_diagnosis/
│   ├── __init__.py
│   ├── provider.py
│   ├── moment_backbone.py
│   └── moment_light_adapt.py
├── compatibility/bearing_v12/
│   └── cloud_moment_exports.py
└── cloud_service/
    ├── moment_backbone.py
    └── moment_light_adapt.py
```

The two `cloud_service` files remain importable but contain no model,
condition-transform, label, or review-policy implementation.

## Dependency Direction

The retained legacy path is:

```text
cloud_service caller
    -> cloud_service MOMENT shim
    -> compatibility.bearing_v12.cloud_moment_exports
    -> scenarios.bearing.cloud_diagnosis implementation
```

The scenario-owned path is:

```text
BearingCloudDiagnosisProvider
    -> BearingCloudHandler
    -> existing cloud inference orchestration
    -> retained legacy MOMENT import boundary
```

Rules:

- generic `cloud_service` modules must not directly import
  `scenarios.bearing`;
- the compatibility module may import the bearing scenario;
- the scenario implementation may depend on generic `CloudSettings` but must
  not import either legacy MOMENT shim;
- compatibility modules and shims must use explicit imports and explicit
  `__all__`; wildcard imports are forbidden;
- each business definition has one physical owner.

`cloud_service.service` remains unchanged in this batch. Its existing import
continues to resolve through the shim, so cloud inference, activation, rollback,
locking, and cached-runner behavior do not change.

## Public Compatibility Contract

The compatibility module explicitly exports the retained names from the two
scenario implementation modules.

Backbone public API:

- `load_moment_backbone`.

LIGHT_ADAPT public API:

- `LABEL_NAMES`;
- `MODEL_VERSION`;
- `MomentPrediction`;
- `MomentReviewPolicy`;
- `build_condition_vector`;
- `deployment_workspace_root`;
- `MomentLightAdaptRunner`.

Required compatibility:

- old and new functions, classes, and dataclasses are the same objects;
- tuple constants preserve their values and object identity through re-export;
- exception types and exact messages remain unchanged;
- `from cloud_service.moment_light_adapt import <name>` and
  `from cloud_service.moment_backbone import load_moment_backbone` continue to
  work;
- no compatibility layer reimplements model or policy behavior.

The implementation objects' `__module__` values intentionally identify the
new scenario location. This is physical ownership metadata, not an external
API, model, or response change.

## Path-Semantics Preservation

`deployment_workspace_root()` currently falls back to the
`cloud_edge_project` directory through its source-file location. A literal
move would change the meaning of `Path(__file__).resolve().parents[1]`.

The scenario-owned implementation must therefore compute the same
`cloud_edge_project` fallback explicitly from its new location. Tests must
cover both branches:

- an ancestor containing an `experiments` directory is returned unchanged;
- when no such ancestor exists, the returned fallback is exactly the project
  root used before migration.

This is the only path-relative adjustment permitted. It preserves existing
behavior rather than introducing a new path policy.

## Behavior Equivalence

### Backbone loader

For an injected fake pipeline class, old and new paths must produce identical:

- `from_pretrained` arguments;
- classification task metadata;
- channel and class counts;
- `init()` invocation;
- warning suppression behavior;
- returned model object;
- propagated errors.

### Condition transform and review policy

Old and new paths must preserve exactly:

- field order for shaft speed, torque, radial load, and temperature;
- statistic order: mean, standard deviation, minimum, maximum;
- 13-element shape and `float32` dtype;
- label order: healthy, outer-ring damage, inner-ring damage;
- state, risk, and recommended-action mappings;
- missing-field and unknown-label error types/messages.

### Runner

Old and new paths must preserve:

- lazy single-load behavior;
- device resolution for `auto`, CPU, and configured devices;
- checkpoint and normalization loading;
- dynamic saved-model import name;
- training-adapter registration in `sys.modules`;
- tensor construction and condition normalization;
- softmax, argmax, probability mapping, confidence, and model version;
- `loaded`, `model_version`, and `gpu_available` semantics;
- exact not-loaded and model-definition import errors.

No model checkpoint, normalization file, deployment file, label expectation,
or floating-point output may be changed to accommodate the move.

## Planned Files

New implementation files:

- `cloud_edge_project/scenarios/bearing/cloud_diagnosis/moment_backbone.py`;
- `cloud_edge_project/scenarios/bearing/cloud_diagnosis/moment_light_adapt.py`.

New compatibility file:

- `cloud_edge_project/compatibility/bearing_v12/cloud_moment_exports.py`.

Modified production files:

- `cloud_edge_project/cloud_service/moment_backbone.py`, converted to a thin
  explicit shim;
- `cloud_edge_project/cloud_service/moment_light_adapt.py`, converted to a thin
  explicit shim;
- `cloud_edge_project/scenarios/bearing/cloud_diagnosis/__init__.py`, only for
  explicit scenario-local exports if required.

Tests:

- add a focused cloud MOMENT layout and equivalence test module under
  `cloud_edge_project/tests/scenarios/bearing/`;
- extend
  `cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py`.

No other production file is planned for modification. If
`cloud_service.service`, model-update orchestration, API routing, or another
production component requires a behavior change, the batch stops for design
review.

## Migration Sequence

1. Freeze the current public symbols, file hashes, path behavior, focused test
   results, full-suite result, and protected worktree status.
2. Add failing layout, ownership, compatibility, and behavior-equivalence
   contracts before moving implementation.
3. Move the backbone loader without logic changes and retain its legacy import
   through the compatibility chain.
4. Move the LIGHT_ADAPT runtime without logic changes except the explicit
   project-root fallback adjustment required by the new file location.
5. Verify runner lifecycle and cloud diagnosis behavior through both legacy
   and scenario paths.
6. Run focused tests, architecture tests, complete regression, diff audit, and
   independent read-only review.

Each implementation move receives its own commit. The next responsibility is
not moved until the current gate passes.

## Verification

### Focused baseline and compatibility

- cloud status runtime tests;
- final MOMENT model configuration tests;
- MOMENT runtime lifecycle tests;
- cloud inference and V1.2 diagnosis tests;
- model-update training tests that consume the backbone loader;
- bearing cloud diagnosis provider and registry tests;
- dependency and portability rules.

### New equivalence coverage

- old/new symbol identity;
- exact constants and policy mappings;
- complete 13D condition-vector value, order, shape, and dtype;
- exact valid and invalid deployment-workspace resolution;
- backbone loader call capture and error propagation;
- unloaded runner error;
- device and GPU-availability behavior;
- model-builder loading and training-adapter installation;
- deterministic fake-tensor prediction output equivalence;
- legacy shim and compatibility-module AST restrictions;
- single-owner source-tree checks.

### Full acceptance

- run the full suite with the established formal model environment;
- require at least the 855-test accepted baseline plus new tests;
- run `git diff --check`;
- inspect every batch commit for scope and protected-worktree isolation;
- verify relevant model and normalization hashes are unchanged;
- request an independent read-only review;
- close every Critical and Important finding before completion.

## Explicit Non-Goals

This batch does not:

- modify `cloud_service.service` inference or runner-cache logic;
- change cloud API requests, responses, status codes, or error payloads;
- alter model activation, distribution, rollback, or persistence;
- move or modify checkpoints, pretrained assets, normalization files, or Git
  LFS configuration;
- move offline trainers or training data adapters;
- move global-analysis implementations;
- move storage schema or repositories;
- change MOMENT architecture, preprocessing, condition normalization,
  softmax, label ordering, confidence, risk, or action mapping;
- delete any legacy module or import path;
- modify protected user-owned worktree entries.

## Stop Conditions

Stop and request direction if:

- old and new condition vectors, probabilities, labels, decisions, errors, or
  path resolution differ;
- a model or normalization asset must change;
- compatibility requires duplicate implementation;
- `cloud_service.service` or model-lifecycle control flow must be rewritten;
- offline training or another Stage 10 responsibility must move in the same
  batch;
- an unrelated user modification overlaps a target file;
- a test failure has no confirmed root cause.

## Success Criteria

- MOMENT backbone and LIGHT_ADAPT runtime implementations have one owner under
  `scenarios.bearing.cloud_diagnosis`.
- Existing `cloud_service.moment_backbone` and
  `cloud_service.moment_light_adapt` imports remain valid through the V1.2
  compatibility module.
- Generic cloud code does not directly import the bearing scenario.
- Public symbols, paths, condition vectors, model loading, prediction,
  decisions, exceptions, and lifecycle integration remain equivalent.
- No model, normalization, API, database, training, or orchestration behavior
  changes.
- Focused and full test suites pass at or above the accepted baseline.
- Independent review reports no remaining Critical or Important issue.
- This batch is reported complete without automatically starting offline
  training, global-analysis, or storage-layout work.
