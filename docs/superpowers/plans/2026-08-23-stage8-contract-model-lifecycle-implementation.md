# Stage 8 Contracts and Model Lifecycle Implementation Plan

## Objective

Connect retained V1.2 result objects to existing scenario-neutral contracts and
make production model-update decisions use a model catalog supplied by the
bearing provider, without changing legacy types, model values, training,
distribution, activation, or rollback behavior.

## Guardrails

- Do not delete or change V1.2 dataclass fields, enums, validations, or exports.
- Do not change model IDs, families, default versions, files, algorithms, or
  weights.
- Do not rewrite training, validation, approval, activation, or rollback state
  machines.
- Keep old direct constructors and imports compatible.
- Do not touch the user's existing deleted or untracked files.
- Complete and report every task before starting the next one.

## Task 1: Freeze the Stage 8 behavioral baseline

1. Run public-schema, model-type, model-update, distribution, activation,
   rollback, registry, and architecture tests with the established formal
   model environment.
2. Record the exact two model descriptors and current default selection.
3. Confirm the working-tree protection list.

Verification: all selected tests pass and no production file changes.

## Task 2: Add generic model lifecycle contracts

Files:

- add `cloud_edge_project/core/model_lifecycle.py`;
- modify `cloud_edge_project/core/scenario_plugin.py`;
- add focused core contract and architecture tests.

Actions:

1. Define immutable `ModelDescriptor` and `ModelCatalog`.
2. Validate identifiers, default membership, descriptor keys, checksum
   declaration, and immutable compatibility metadata.
3. Add `model_catalog()` to `ModelUpdateProvider`.
4. Prove a synthetic non-bearing catalog works without scenario imports.

## Task 3: Move bearing model metadata behind its provider

Files:

- add `scenarios/bearing/cloud/model_update/model_catalog.py`;
- modify the bearing model-update provider;
- modify `cloud_service/model_update/model_types.py` as a compatibility view;
- add catalog and legacy-export tests.

Actions:

1. Declare the unchanged `distilled_h5` and `moment_light_adapt` descriptors.
2. Keep `ModelTypeSpec`, `MODEL_TYPE_SPECS`, `VALID_MODEL_TYPES`, and
   `validate_model_type` compatible.
3. Return and inject the catalog from `BearingModelUpdateProvider`.

Verification: old and new catalog values are field-for-field equal.

## Task 4: Inject the selected catalog into model-update decisions

Files:

- modify `cloud_service/model_update/service.py`;
- modify `cloud_service/model_update/distribution_client.py`;
- update model-update and distribution tests.

Actions:

1. Add an optional catalog to `ModelUpdateService`, defaulting to the retained
   compatibility catalog.
2. Use the catalog for missing model type, validation, baseline version,
   family routing, and rollback routing.
3. Pass the catalog into distribution helpers.
4. Verify a synthetic catalog works without changing generic service code.

Verification: all existing model-update payloads, errors, targets, and state
transitions remain unchanged.

## Task 5: Add lossless V1.2 diagnosis and decision mappings

Files:

- add `compatibility/bearing_v12/diagnosis_mapper.py`;
- add round-trip tests for all four retained result types.

Actions:

1. Map edge and cloud results to `ScenarioDiagnosis`.
2. Map bearing and device decisions to `ScenarioDecision`.
3. Store legacy-only identity/lifecycle data as immutable evidence.
4. Reverse-map with a same-type template using dataclass replacement.
5. Reject wrong template types.

Verification: unmodified round trips are exactly equal; changed generic
conclusions alter only documented common fields.

## Task 6: Centralize legacy public exports

Files:

- add `compatibility/bearing_v12/legacy_exports.py`;
- modify `common/schemas.py`;
- add legacy import and object-identity tests.

Actions:

1. Re-export retained V1.2 result types and bearing signal validators from the
   compatibility module.
2. Route `common.schemas` through that module.
3. Preserve callable identity, messages, shapes, constants, and old paths.
4. Add an architecture rule against direct `scenarios.bearing` imports from
   `common.schemas`.

## Task 7: Run focused and full regression

1. Run all Stage 8 contract, round-trip, model-update, distribution,
   activation, rollback, legacy-schema, registry, and architecture tests.
2. Run `git diff --check` and verify no unintended legacy contract/model file
   changes.
3. Run the complete repository suite with formal model paths.
4. Commit only Stage 8 implementation and tests.

Acceptance: full suite is at least 734 tests plus new Stage 8 tests.

## Task 8: Independent review and final acceptance

1. Request a read-only review against the approved design and implementation
   plan.
2. Fix every Critical and Important finding.
3. Rerun affected tests and the complete suite after fixes.
4. Confirm only user-owned changes remain outside Stage 8 commits.
5. Report Stage 8 complete without automatically entering Stage 9.

Acceptance: reviewer reports no remaining Critical or Important issue and all
verification remains green.
