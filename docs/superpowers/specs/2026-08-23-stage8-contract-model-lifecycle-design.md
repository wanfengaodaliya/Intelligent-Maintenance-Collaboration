# Stage 8 Public Contracts and Model Lifecycle Design

## Goal

Use the existing scenario-neutral diagnosis and decision contracts at the V1.2
compatibility boundary, and move model identity, family, default-version, and
compatibility knowledge behind an injected scenario model catalog. Preserve
all bearing types, legacy imports, training behavior, activation behavior, and
rollback behavior.

## Scope

Stage 8 will:

- add lossless adapters between retained V1.2 result types and the existing
  `ScenarioDiagnosis` / `ScenarioDecision` contracts;
- centralize retained bearing exports in a V1.2 compatibility module;
- add scenario-neutral model descriptor and catalog contracts;
- make the production model-update service use a catalog supplied by the
  bearing provider;
- retain old model-type names and direct construction behavior through a
  compatibility catalog;
- add round-trip, model-catalog, legacy-export, architecture, and behavioral-
  equivalence tests.

Stage 8 will not:

- delete, rename, or physically move `BearingLifecycleStatus`,
  `EdgeBearingResult`, `CloudBearingResult`, `BearingDecisionResult`, or
  `DeviceDecisionResult`;
- change any V1.2 dataclass field, enum value, validation rule, or serialization
  shape;
- rewrite training, validation, approval, distribution, activation, or
  rollback state machines;
- change model algorithms, checkpoints, input features, labels, thresholds,
  versions, files, or Git LFS behavior;
- remove old imports from `common.schemas` or
  `cloud_service.model_update.model_types`;
- add automatic activation, rollback, migration, or cleanup behavior.

## Existing Baseline

- Full repository regression after Stage 7: 734 passing tests.
- `core/scenario_contracts.py` already provides immutable, scenario-neutral
  `ScenarioDiagnosis` and `ScenarioDecision`; they are not yet connected to
  the V1.2 result types.
- `core/diagnosis_contracts.py` remains the stable V1.2 public contract and is
  used throughout the edge lifecycle and persistence paths.
- `common.schemas` directly re-exports bearing signal validation functions
  from the bearing scenario to preserve old imports.
- `cloud_service/model_update/model_types.py` contains the two bearing model
  IDs, their families, and default versions.
- `BearingModelUpdateProvider` already assembles the production model-update
  service and owns cloud activation callbacks, making it the appropriate
  injection boundary.
- The focused exploration suite produced 68 passes and one environment-only
  failure when the formal MOMENT deployment path was not supplied. The
  established complete test environment remains the acceptance environment.

## Considered Approaches

### Chosen: adapters plus an injected catalog and legacy shims

Reuse the existing generic result types, add explicit compatibility mappers,
and inject a scenario catalog into the production model-update service. Keep
the old globals and constructors as compatibility views over the bearing
catalog.

This changes dependency direction without replacing stable contracts or
rewriting lifecycle workflows.

### Rejected: aliases only

Adding type aliases would preserve imports but leave the cloud model-update
path responsible for bearing model IDs, default versions, and families. It
would not solve the coupling.

### Rejected: full legacy-type and training replacement

Replacing the V1.2 types or training pipeline would expand the stage into a
state-machine and persistence rewrite. That violates the approved scope and
would create unnecessary compatibility risk.

## Architecture

### Generic diagnosis and decision boundary

The existing `ScenarioDiagnosis` and `ScenarioDecision` remain the only new
generic result types. Stage 8 will not introduce a parallel result hierarchy.

`compatibility/bearing_v12/diagnosis_mapper.py` will provide explicit
conversions for:

- `EdgeBearingResult` to and from `ScenarioDiagnosis`;
- `CloudBearingResult` to and from `ScenarioDiagnosis`;
- `BearingDecisionResult` to and from `ScenarioDecision`;
- `DeviceDecisionResult` to and from `ScenarioDecision`.

Forward mapping places the common conclusion in typed generic fields. Identity
or lifecycle data with no generic field remains in immutable evidence. Reverse
mapping requires the original V1.2 object as a template and uses dataclass
replacement to update only common conclusion fields. This guarantees that
result IDs, revisions, device/task/sender identity, window ranges, packet IDs,
timestamps, review state, source fields, and other legacy-only data are not
invented or lost.

The compatibility mapper owns the fixed mapping from the two retained bearing
model families to generic `model_id` values. Generic contracts do not contain
those names.

### Legacy exports

`compatibility/bearing_v12/legacy_exports.py` becomes the explicit place that
re-exports retained bearing result types and signal-contract functions.
`common.schemas` imports its compatibility exports from this module instead of
directly importing `scenarios.bearing`.

External imports, callable identities, validation messages, JSON shapes, and
error codes remain unchanged. The compatibility module may import the bearing
scenario; core and generic common logic may not.

### Generic model lifecycle contracts

`core/model_lifecycle.py` will define:

- immutable `ModelDescriptor` with `model_id`, `family`, `default_version`,
  `description`, `checksum_algorithm`, and compatibility declarations;
- immutable `ModelCatalog` with `scenario_id`, `default_model_id`, and a frozen
  mapping of model descriptors;
- catalog validation and lookup that do not know bearing, H5, or MOMENT names.

The catalog validates non-empty identifiers, descriptor-key consistency,
presence of the default model, SHA-256 as the current checksum declaration,
and immutable compatibility metadata. It does not inspect model files or
perform activation.

The existing `ModelUpdateProvider` protocol gains `model_catalog()`. Its
existing `activate_candidate` and `activate_version` operations remain the
activation and rollback boundary; rollback continues to reactivate a retained
version through the current callback path.

### Bearing model catalog

`scenarios/bearing/cloud/model_update/model_catalog.py` provides the two
current descriptors without changing their values:

- `distilled_h5`, family `edge`, with its current default version;
- `moment_light_adapt`, family `cloud`, with its current default version.

Compatibility declarations record the existing feature/model family
expectations only; they do not add a new enforcement gate in this stage.

`BearingModelUpdateProvider.model_catalog()` returns this catalog and passes
it into `ModelUpdateService` during production assembly.

### Model-update compatibility

`ModelUpdateService` accepts an optional `ModelCatalog`. The production
bearing provider supplies one. Direct legacy construction receives the same
bearing compatibility catalog, preserving existing tests and external callers.

The service uses the selected catalog for:

- default model ID when `model_type` is absent;
- model-type validation;
- default baseline version;
- edge/cloud family decisions during distribution and rollback.

Distribution helpers accept an optional catalog and preserve their current
default behavior. `cloud_service.model_update.model_types` continues exporting
`ModelTypeSpec`, `MODEL_TYPE_SPECS`, `VALID_MODEL_TYPES`,
`validate_model_type`, and `ActiveModelVersionStore`; the first four become
compatibility views/helpers backed by the bearing catalog. The version store
and its database behavior remain unchanged.

Training registries, trainers, dataset preparation, metrics, and persisted
training plans are not redesigned. They remain behind the existing bearing
model-update provider and are candidates for later physical organization only.

## Data Flow

### V1.2 diagnosis compatibility

1. A retained V1.2 result is passed to the compatibility mapper.
2. The mapper creates a generic diagnosis or decision with immutable evidence.
3. A generic caller reads scenario-neutral conclusion fields.
4. When a V1.2 response is required, the mapper applies the generic conclusion
   to the original template.
5. The reconstructed object must equal the original when no conclusion was
   changed.

No edge lifecycle, persistence, API, or MQTT path is switched to a new stored
type in this stage.

### Model update

1. Cloud scenario assembly creates `BearingModelUpdateProvider`.
2. The provider supplies the bearing `ModelCatalog` when constructing
   `ModelUpdateService`.
3. The service resolves validation, default version, and family through the
   catalog.
4. Existing dataset, training, validation, approval, and distribution code
   runs with unchanged payloads.
5. Existing provider activation callbacks activate a candidate or a previous
   version.
6. Old direct constructors obtain the same values through the legacy catalog.

## Error Handling and Compatibility

- A missing `model_type` still selects `distilled_h5` through the bearing
  catalog's `default_model_id`.
- Unknown model types retain `UNSUPPORTED_MODEL_TYPE=<value>`.
- Invalid distribution requests retain `INVALID_APPROVED_MODEL`.
- Training, validation, approval, activation, and rollback error codes remain
  unchanged.
- Catalog lookup errors are translated at the same existing service boundary;
  no new API status or response field is introduced.
- Old V1.2 type imports and `common.schemas` signal-validator imports remain
  valid.
- Reverse diagnosis mapping rejects a template of the wrong legacy type rather
  than fabricating missing identity.
- No model artifact is read, written, activated, or rolled back merely by
  constructing a catalog.

## Planned Files

New files:

- `cloud_edge_project/core/model_lifecycle.py`
- `cloud_edge_project/compatibility/bearing_v12/diagnosis_mapper.py`
- `cloud_edge_project/compatibility/bearing_v12/legacy_exports.py`
- `cloud_edge_project/scenarios/bearing/cloud/model_update/model_catalog.py`
- focused contract, round-trip, catalog, compatibility, and architecture tests.

Existing files changed only where required:

- `cloud_edge_project/core/scenario_plugin.py` to expose the catalog from the
  existing model-update provider protocol;
- `cloud_edge_project/common/schemas.py` to route retained bearing exports
  through the compatibility module;
- `cloud_edge_project/cloud_service/model_update/model_types.py` to retain old
  names as catalog-backed compatibility exports;
- `cloud_edge_project/cloud_service/model_update/service.py` to use its injected
  catalog;
- `cloud_edge_project/cloud_service/model_update/distribution_client.py` to use
  the selected catalog while preserving the default;
- `cloud_edge_project/scenarios/bearing/cloud/model_update/provider.py` to own
  and inject the bearing catalog;
- focused registry and architecture tests where needed.

If an existing file does not need modification after implementation
inspection, it remains untouched. No unrelated cleanup or formatting is
allowed.

## Verification

### Result round trips

For representative edge, cloud, bearing-decision, and device-decision objects:

- forward mapping produces the expected generic fields;
- evidence is immutable;
- reverse mapping with the original template is field-for-field equal;
- modified generic conclusions update only the documented common fields;
- wrong-template types fail explicitly.

### Model catalog and lifecycle

- The bearing catalog contains exactly the two current model IDs.
- Families, descriptions, and default versions match the Stage 7 globals.
- The default model remains `distilled_h5`.
- A synthetic non-bearing catalog can be validated and used by generic lookup
  and distribution helpers without changing core code.
- Model creation, default baseline selection, distribution target, candidate
  activation, version activation, and rollback outputs remain equivalent.

### Legacy compatibility

- Old imports from `core.diagnosis_contracts`, `common.schemas`, and
  `cloud_service.model_update.model_types` remain valid.
- Re-exported signal functions retain object identity with the bearing
  implementation.
- Existing validation errors and serialized shapes remain unchanged.

### Architecture

- `core/model_lifecycle.py` contains no bearing, H5, MOMENT, or scenario-plugin
  imports.
- `common.schemas` no longer directly imports `scenarios.bearing`.
- Generic model-update service code contains no literal bearing model IDs after
  dependency injection, except explicit compatibility defaults where required
  by the retained old API.

Focused tests must pass before the complete repository suite. Stage 8 is
accepted only if the full suite is at least the current 734-test baseline plus
new tests, using the established formal model environment.

## Success Criteria

- Generic result contracts can represent and losslessly round-trip retained
  V1.2 conclusions through explicit compatibility adapters.
- No old V1.2 type, field, enum, import, JSON shape, or validation behavior is
  removed or changed.
- Production model-update decisions use a provider-supplied catalog.
- The two bearing model IDs, families, and default versions are unchanged.
- Model training, activation, distribution, and rollback behavior remains
  equivalent.
- Core model lifecycle contains no bearing implementation knowledge.
- Full regression, round-trip, compatibility, and architecture checks pass.
