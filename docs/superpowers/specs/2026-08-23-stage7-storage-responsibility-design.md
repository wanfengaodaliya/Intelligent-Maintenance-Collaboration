# Stage 7 Storage Responsibility Decoupling Design

## Goal

Make scenario storage an executable plugin capability without changing the
existing SQLite schema, migrations, repositories, historical data, or query
behavior. This stage changes the registration entry point only; it does not
physically move or rewrite SQL.

## Scope

Stage 7 will:

- define a scenario-neutral storage registration boundary;
- add a bearing storage provider that registers the existing schema script;
- resolve the storage provider through the cloud scenario registry;
- inject the provider into the central cloud database initialization path;
- retain the current direct `initialize_database(database_path)` behavior for
  repositories, tests, tools, and legacy callers;
- add schema, history, write, idempotency, registry, and architecture tests.

Stage 7 will not:

- rename, remove, recreate, or physically relocate any table, column, index,
  constraint, or SQL definition;
- change migration functions, migration versions, transaction boundaries,
  SQLite PRAGMA settings, or initialization order;
- change repository APIs, queries, serialization, or business meanings;
- require a destructive migration, database reset, or history rewrite;
- change cloud APIs, edge behavior, MQTT, models, scheduling, consistency, or
  arbitration;
- remove the legacy no-provider initialization path.

## Existing Baseline

- Full repository regression after Stage 6: 728 passing tests.
- `cloud_service/storage/database.py` owns connection setup, migrations, and
  the single call that executes the complete legacy schema.
- `cloud_service/storage/schema.py` contains the current DDL in its verified
  physical order.
- Existing repositories call `initialize_database(database_path)` directly.
- The bearing manifest declares `STORAGE_PROVIDER`, but the plugin does not
  resolve an executable provider and the cloud role does not request it.
- User-owned working-tree deletions and untracked documents are outside this
  stage and must remain untouched.

## Considered Approaches

### Chosen: explicit provider injection with a legacy fallback

Add an optional provider collection to `initialize_database`. The cloud
application resolves the bearing provider once and passes it during central
startup. When no provider is supplied, initialization executes the existing
DDL exactly as it does today.

This is the smallest change that proves the plugin boundary while preserving
all direct repository and test callers.

### Rejected: global mutable provider registry

A process-global registry would reduce constructor changes, but it would make
database behavior depend on import and startup order, contaminate tests, and
introduce shared mutable state.

### Deferred: immediate platform/bearing SQL split

Separating every statement into new physical files would make ownership more
visible, but it risks changing schema execution order and migration behavior.
The approved scope explicitly defers this move until the registration path is
verified.

## Architecture

### Generic registration contract

`core/scenario_plugin.py` will define a small `StorageRegistrar` protocol that
exposes only schema-script execution. `StorageProvider.initialize(registrar)`
will depend on that protocol rather than SQLite connections or cloud storage
implementations.

The contract will not expose bearing vocabulary, table names, repository
classes, filesystem mutation, or destructive migration operations.

### Cloud storage registrar

`cloud_service/storage/database.py` will implement a private registrar around
the active SQLite connection. At the exact point where the current
`connection.executescript(DDL)` runs:

- with explicit providers, it invokes each provider through the registrar;
- without providers, it executes the current `DDL` directly.

All work before and after that point remains in the existing order. The
connection setup, transaction handling, pre-DDL migrations, post-DDL
migrations, and migration-record writes are unchanged.

### Bearing storage provider

`scenarios/bearing/storage/` will contain `BearingStorageProvider`. It will
delegate to `compatibility/bearing_v12/storage_mapper.py`, which registers the
existing `cloud_service.storage.schema.DDL` text without copying, editing, or
moving it.

During this transitional stage the legacy DDL still includes the complete
schema used by the bearing deployment, including platform-adjacent tables.
That is intentional: table-by-table physical ownership is deferred, while the
executable registration responsibility moves behind the bearing capability.

### Scenario assembly

- `BearingScenarioPlugin` resolves `STORAGE_PROVIDER` only when requested.
- `CLOUD_CAPABILITIES` includes `STORAGE_PROVIDER`.
- `cloud_service/app.py` resolves the provider from the already-built cloud
  scenario registry and injects it into central lifespan initialization.
- Providers are constructed during scenario assembly, never per row or query.

Existing repository constructors and independent tools remain compatible by
using the no-provider fallback. They do not import the bearing plugin.

## Initialization Data Flow

### Production cloud startup

1. Cloud scenario assembly creates the bearing storage provider.
2. The application lifespan resolves `STORAGE_PROVIDER` for the configured
   scenario.
3. `initialize_database` opens the database with the existing PRAGMA values.
4. Existing pre-DDL migrations run.
5. The bearing provider registers the unchanged legacy DDL through the generic
   registrar.
6. Existing post-DDL migrations and migration-record writes run.
7. Startup continues only after successful initialization.

### Legacy and direct repository path

1. A repository, test, or tool calls `initialize_database(database_path)`.
2. No provider is supplied.
3. The function executes the same legacy DDL at the same point in the
   migration sequence.
4. The caller observes Stage 6 behavior with no API change.

## Error Handling and Compatibility

- A provider exception propagates through initialization and aborts cloud
  startup, matching current schema-initialization failure behavior.
- Registration is not silently skipped and does not fall back after an
  explicitly supplied provider fails.
- Repeated initialization relies on the existing `IF NOT EXISTS`, direct
  migration checks, and conflict-safe migration records.
- Multiple providers, when supplied, run in deterministic caller order.
- An empty explicit provider collection is invalid; callers must either omit
  the argument for legacy behavior or provide at least one provider. This
  prevents a silently schema-less database.
- Historical rows are not rewritten except by the unchanged existing
  migrations.
- Existing table names, columns, indexes, constraints, migration descriptions,
  and serialized JSON remain unchanged.

## Planned Files

New files:

- `cloud_edge_project/scenarios/bearing/storage/__init__.py`
- `cloud_edge_project/scenarios/bearing/storage/provider.py`
- `cloud_edge_project/compatibility/bearing_v12/storage_mapper.py`
- focused storage registration and equivalence tests.

Existing files changed only where required:

- `cloud_edge_project/core/scenario_plugin.py` for the generic registrar and
  precise storage-provider protocol;
- `cloud_edge_project/cloud_service/storage/database.py` for optional provider
  execution at the existing DDL boundary;
- `cloud_edge_project/scenarios/bearing/plugin.py` to resolve the provider;
- `cloud_edge_project/bootstrap/scenarios.py` to include storage in the cloud
  role;
- `cloud_edge_project/cloud_service/app.py` to inject the resolved provider at
  central startup;
- registry and architecture tests for the new executable capability.

If implementation inspection shows that a listed runtime file need not
change, it will remain untouched. No unrelated cleanup or formatting is
allowed.

## Verification

### Schema equivalence

Create fresh databases through both initialization paths and compare normalized
`sqlite_master` rows for every table, index, trigger, and view. Compare
`PRAGMA table_info`, `PRAGMA index_list`, and foreign-key metadata for all
tables. Provider and legacy schemas must be identical.

### Historical database compatibility

Create or copy a representative pre-Stage-7 database containing historical
rows, initialize it through the provider path, and verify:

- all rows remain readable;
- primary identifiers and serialized results are unchanged;
- existing repository queries return the same values;
- no table or index is lost.

### Write and idempotency compatibility

- Write representative new results through existing repositories after
  provider initialization and read them back unchanged.
- Initialize the same database twice through the provider path.
- Verify no duplicate business rows or migration records and no schema drift.

### Registry and architecture checks

- The full registry and cloud registry resolve the bearing storage provider.
- Edge and sender registries do not resolve storage.
- Generic core and cloud storage initialization do not import
  `scenarios.bearing`.
- The provider is resolved during assembly and is not created in a high-
  frequency query or persistence path.

Focused tests must pass before the complete repository suite. Stage 7 is
accepted only if the full suite is at least the current 728-test baseline plus
new tests, with no unexplained schema or behavior difference.

## Success Criteria

- The cloud startup path initializes storage through the registered bearing
  provider.
- The no-provider initialization API remains fully compatible.
- Provider and legacy initialization produce identical SQLite metadata.
- Historical databases remain readable and writable without destructive
  migration.
- Repeated initialization is idempotent and creates no duplicate data.
- No SQL text, table name, column, index, constraint, migration, repository
  query, or business behavior changes.
- Full regression, schema-equivalence, and architecture checks pass.
