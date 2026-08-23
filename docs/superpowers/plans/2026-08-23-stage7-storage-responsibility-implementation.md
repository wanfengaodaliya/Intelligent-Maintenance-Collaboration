# Stage 7 Storage Responsibility Implementation Plan

## Objective

Route central cloud schema initialization through a registered scenario
`StorageProvider`, while preserving the exact legacy schema, migration order,
direct initialization API, repositories, and historical data behavior.

## Guardrails

- Do not edit SQL text in `cloud_service/storage/schema.py`.
- Do not rename or move tables, columns, indexes, constraints, or migrations.
- Do not alter repository APIs or queries.
- Do not add destructive migration or cleanup behavior.
- Do not touch the user's existing deleted or untracked files.
- Complete and report each task before starting the next one.

## Task 1: Freeze the Stage 7 storage baseline

Files:

- existing cloud storage and migration tests only; no production edits.

Actions:

1. Run focused existing storage, model-update storage, arbitration persistence,
   and scenario-registry tests with the project Python path.
2. Create a fresh database using the legacy initializer and capture normalized
   `sqlite_master`, table metadata, index metadata, migration rows, and an
   initialization-twice result.
3. Confirm the working-tree protection list before implementation.

Verification:

- all selected tests pass;
- the baseline database can be initialized twice;
- no production file changes.

## Task 2: Define and test the generic storage registration contract

Files:

- modify `cloud_edge_project/core/scenario_plugin.py`;
- add focused contract tests.

Actions:

1. Define `StorageRegistrar.execute_schema(script: str)`.
2. Change `StorageProvider.initialize` to accept `StorageRegistrar`.
3. Use a fake registrar/provider test to prove delegation without importing
   SQLite or the bearing scenario into core.

Verification:

- contract tests pass;
- architecture tests show no bearing dependency in core.

## Task 3: Add provider-aware initialization with exact legacy fallback

Files:

- modify `cloud_edge_project/cloud_service/storage/database.py`;
- add storage initialization equivalence tests.

Actions:

1. Add a private connection-backed registrar.
2. Add an optional `storage_providers` argument to `initialize_database`.
3. Invoke explicit providers at the current `executescript(DDL)` position.
4. Preserve direct `DDL` execution when the argument is omitted.
5. Reject an explicitly empty provider collection.
6. Keep every migration call and migration record in its existing order.

Verification:

- fake-provider execution test passes;
- the no-provider path remains compatible;
- existing migration tests pass.

## Task 4: Add the bearing storage provider and compatibility mapper

Files:

- add `cloud_edge_project/scenarios/bearing/storage/__init__.py`;
- add `cloud_edge_project/scenarios/bearing/storage/provider.py`;
- add `cloud_edge_project/compatibility/bearing_v12/storage_mapper.py`;
- add provider/schema equivalence tests.

Actions:

1. Keep the existing DDL in its current file.
2. Make the compatibility mapper submit that exact DDL string to the generic
   registrar.
3. Make `BearingStorageProvider` delegate to the mapper.
4. Build legacy-path and provider-path databases and compare normalized schema
   metadata field for field.

Verification:

- identical `sqlite_master`, table, index, and foreign-key metadata;
- no diff in `schema.py`.

## Task 5: Resolve and inject storage through scenario assembly

Files:

- modify `cloud_edge_project/scenarios/bearing/plugin.py`;
- modify `cloud_edge_project/bootstrap/scenarios.py`;
- modify `cloud_edge_project/cloud_service/app.py`;
- update scenario registry and architecture tests.

Actions:

1. Resolve `STORAGE_PROVIDER` only when requested.
2. Add storage to `CLOUD_CAPABILITIES`, not edge or sender capabilities.
3. Resolve the provider once from the cloud registry.
4. Pass it to central lifespan database initialization.
5. Keep repository constructors and independent tools on the legacy fallback.

Verification:

- full/cloud registries resolve storage;
- edge/sender registries do not;
- cloud app has no direct bearing import;
- existing cloud startup behavior remains valid.

## Task 6: Prove history, write, and idempotency compatibility

Files:

- extend the focused storage equivalence tests only.

Actions:

1. Seed representative legacy rows before provider initialization.
2. Compare reads through existing repositories before and after initialization.
3. Write representative new records after provider initialization and read
   them back through existing repositories.
4. Initialize the same database twice through the provider path.
5. Assert business-row counts, migration uniqueness, and schema metadata stay
   unchanged.

Verification:

- all history/write/idempotency assertions pass;
- no duplicate data or schema drift.

## Task 7: Run final regression and independent review

Actions:

1. Run all focused storage, registry, architecture, API, arbitration,
   global-analysis, and model-update tests.
2. Run `git diff --check`.
3. Run the full repository test suite with the established model environment.
4. Commit only Stage 7 implementation and tests.
5. Request an independent read-only code review against the approved design.
6. Fix all Critical and Important findings, rerun affected tests, then rerun
   the full suite.

Acceptance:

- full suite is at least 728 tests plus new Stage 7 tests;
- independent reviewer reports no remaining Critical or Important issue;
- only the user's pre-existing deletions and untracked documents remain outside
  Stage 7 commits;
- Stage 7 is reported complete without automatically entering Stage 8.
