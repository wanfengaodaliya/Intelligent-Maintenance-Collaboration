# Stage 10 Edge H5 Client and Probe Physical Layout Design

## Goal

Complete the next Stage 10 physical-layout batch by moving the concrete bearing
H5 runtime client and its activation probe loader into the bearing scenario
package. Preserve the existing edge model lifecycle, model-store, download,
activation-pointer, rollback, routes, errors, resources, and legacy imports.

This batch closes the responsibility left intentionally out of the accepted H5
algorithm batch. It does not reopen or modify the already accepted H5 feature,
network, or diagnostic-runner implementation.

## Existing Boundary

The H5 algorithm is already owned by
`scenarios.bearing.edge_inference.h5`. Two bearing-specific runtime modules
remain under the generic edge package:

- `edge_model/local_h5_client.py` owns H5 constants, class-label validation,
  the H5 model factory, probe validation, and the concrete runtime client;
- `edge_model/h5_probe.py` owns bearing channel names, sample contracts, probe
  identities, manifest validation, and conversion to a bearing inference task.

The platform modules `model_store.py`, `version_store.py`, `model_pull.py`, and
`edge_runtime/model_update_poller.py` own generic model distribution and
lifecycle mechanics and must remain outside the scenario package.

## Chosen Approach

Use “scenario-owned concrete runtime, platform-owned lifecycle mechanics.”

Move the concrete H5 client and probe implementation into
`scenarios.bearing.edge_inference`, retain the old `edge_model` files as
explicit compatibility shims, and make the bearing Provider assemble the new
scenario objects directly. Add a scenario-neutral activation error in the core
model-lifecycle contract so the generic poller no longer imports an H5 error.

This is preferred over two alternatives:

- moving only the probe would leave H5 labels, model type, and validation in
  the generic client;
- introducing a new universal atomic-model-client framework would rewrite a
  stable lifecycle path and add unnecessary abstraction and regression risk.

## Responsibility Split

### Bearing scenario owns

- `H5_RUNTIME_MODEL_VERSION` and `MODEL_TYPE_DISTILLED_H5` compatibility
  constants;
- H5 diagnosis-label and probability validation;
- `LocalH5ClientConfig`, `_H5ModelHandle`, and `LocalH5ModelClient`;
- loading `DistilledH5DiagnosticModel`;
- H5 probe manifest, artifact, channel, dtype, shape, hash, and task checks;
- H5 probe resource path calculation;
- H5-specific activation failures through `H5ActivationError`.

### Platform retains

- model directory and active-version pointer operations;
- bundled baseline seeding and manifest validation;
- model download and signature verification;
- polling, reporting, retry, rollback orchestration, and state persistence;
- the generic runtime-client protocol;
- a scenario-neutral `ModelActivationError` base contract.

`H5ActivationError` will subclass `ModelActivationError`. Existing direct H5
client calls continue to raise `H5ActivationError`; the generic poller catches
the base contract and uses it for its own scenario-neutral confirmation errors.
Stable error text and error-code extraction remain unchanged.

## Target Layout

```text
cloud_edge_project/
├── core/
│   └── model_lifecycle.py
├── scenarios/bearing/edge_inference/
│   ├── provider.py
│   ├── local_h5_client.py
│   ├── h5_probe.py
│   └── h5/                         # accepted algorithm implementation
├── compatibility/bearing_v12/
│   └── edge_h5_runtime_exports.py
└── edge_service/src/
    ├── edge_model/
    │   ├── local_h5_client.py       # explicit legacy shim
    │   ├── h5_probe.py              # explicit legacy shim
    │   ├── model_store.py           # unchanged platform mechanism
    │   └── version_store.py         # unchanged platform mechanism
    └── edge_runtime/
        └── model_update_poller.py   # generic activation error only
```

No resource, weight, manifest, model directory, active pointer, or Git LFS path
moves in this batch.

## Dependency Direction

The production path becomes:

```text
edge_service/app.py
    -> scenario registry
    -> BearingEdgeInferenceProvider
    -> scenarios.bearing.edge_inference.local_h5_client
    -> scenarios.bearing.edge_inference.h5
```

The concrete client may depend on generic edge lifecycle helpers:

```text
scenario local_h5_client
    -> edge_model.model_store/version_store contracts
```

The legacy path remains:

```text
edge_model.local_h5_client / edge_model.h5_probe
    -> compatibility.bearing_v12.edge_h5_runtime_exports
    -> scenarios.bearing.edge_inference
```

The generic poller depends only on `core.model_lifecycle.ModelActivationError`
and the runtime protocol. It must not import the scenario or compatibility
package.

## Probe Resource Location

The committed probe remains at
`edge_service/resources/model_probes/distilled_h5/v1`. The scenario-owned
`default_probe_dir()` derives the project/application root from its module path
and then resolves the unchanged `edge_service/resources` location. This layout
works both in the source checkout and in the current `/app` container layout,
where `scenarios` and `edge_service` are sibling directories.

Tests will freeze the resolved source and container-relative shape. No probe
NPZ or manifest is regenerated, copied, renamed, or edited.

## Compatibility Contract

The old paths retain their existing public names and object identity:

- `edge_model.local_h5_client` continues to export the H5 constants,
  `H5ActivationError`, `LocalH5ClientConfig`, and `LocalH5ModelClient`;
- `edge_model.h5_probe` continues to export the probe constants,
  `H5ProbeError`, `default_probe_dir`, `read_probe_manifest`, `load_h5_probe`,
  and `load_h5_probe_task`;
- old and scenario paths resolve to the same retained classes and functions;
- old pickle globals and cold import orders remain valid;
- direct construction, test injection, readiness, inference, activation, and
  rollback produce the same observable values and errors.

The legacy modules contain only a docstring, explicit imports, and `__all__`.
No wildcard import or duplicated implementation is allowed.

## Runtime and Error Flow

1. The Provider initializes the unchanged model store and selects the same
   initial version.
2. It constructs the scenario-owned client with the same configuration.
3. The client loads the already scenario-owned H5 diagnostic runner.
4. Inference snapshots the same handle, produces the same result, and applies
   the same version guard.
5. Activation loads the candidate, loads the unchanged probe resource, runs
   the same validation, persists the same active pointer, and swaps the handle
   atomically.
6. Failures preserve exact H5 error types for direct calls and exact stable
   error messages/codes for poller reporting.

No retry, lock, timeout, fallback, persistence, or exception-swallowing rule
changes.

## Testing Strategy

### Baseline and goldens

- freeze the current client/probe source and artifact hashes;
- run the existing H5 route, lifecycle, worker, Provider, and fixed-probe
  tests before production changes;
- freeze readiness, inference, activation, rollback, probe task, manifest, and
  error outputs.

### Layout contracts

- require single implementation owners under the bearing scenario;
- require old modules to be explicit thin shims;
- require old/compatibility/scenario object identity;
- verify cold imports and legacy pickle globals;
- verify the generic poller imports no H5 or bearing implementation;
- verify Provider source directly assembles the scenario client;
- verify the probe path resolves to the unchanged resource directory.

### Behavioral regression

- compare exact probe manifest/task construction and all covered errors;
- compare client configuration, readiness, model loading, evidence, inference,
  handle isolation, activation, rollback, and pointer recovery;
- run weak-network and worker-guard tests;
- run edge Provider, architecture, Docker import, and model-update lifecycle
  neighbors;
- run the complete suite with the formal model environment;
- request independent review and close every Critical or Important finding.

## Planned Files

Add:

- `scenarios/bearing/edge_inference/local_h5_client.py`;
- `scenarios/bearing/edge_inference/h5_probe.py`;
- `compatibility/bearing_v12/edge_h5_runtime_exports.py`;
- focused layout and compatibility tests.

Modify:

- `core/model_lifecycle.py` for `ModelActivationError`;
- `scenarios/bearing/edge_inference/provider.py` for direct scenario assembly;
- `edge_service/src/edge_model/local_h5_client.py` and `h5_probe.py` into
  explicit shims;
- `edge_service/src/edge_runtime/model_update_poller.py` to use the generic
  activation error;
- architecture tests.

No Dockerfile change is expected because both `scenarios` and `compatibility`
are already copied into the edge image.

## Explicit Non-Goals

This batch does not:

- change the H5 network, features, preprocessing, labels, thresholds, weights,
  probability calculation, or evidence;
- change model-store, version-store, pull, signature, pointer, poll interval,
  reporting, retry, rollback, or health behavior;
- move probe resources, model assets, manifests, normalization files, or Git
  LFS content;
- modify cloud model update, offline training, storage, scheduler, cloud
  diagnosis, or global analysis;
- delete legacy imports or user-owned worktree entries.

## Stop Conditions

Stop and request direction if:

- the fixed probe, model output, version, readiness, activation, rollback, or
  error behavior differs;
- the resource must move or the Docker layout must change;
- preserving compatibility requires duplicate implementation;
- model-store or poller algorithms must be rewritten;
- a target overlaps user-owned changes;
- another Stage 10 responsibility must move in the same batch.

## Success Criteria

- the H5 client and probe each have one production implementation owner under
  `scenarios.bearing.edge_inference`;
- the Provider directly creates the scenario-owned client;
- the generic poller no longer imports H5-specific errors;
- old client/probe imports, pickle globals, resources, outputs, errors, and
  lifecycle behavior remain compatible;
- focused, architecture, neighbor, and full tests pass;
- all protected worktree entries remain untouched;
- independent review reports no remaining Critical or Important issue.
