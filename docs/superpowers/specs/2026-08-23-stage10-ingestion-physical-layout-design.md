# Stage 10 Bearing Ingestion Physical Layout Design

## Goal

Complete the first, independently accepted batch of Stage 10 physical layout
cleanup by moving the already provider-wrapped bearing ingestion
implementations out of the generic sender package and into the bearing
scenario package. Preserve every existing sender import, payload, validation,
windowing, serialization, source-mapping, and database behavior.

Stage 10 contains several independent high-risk responsibility groups. This
design covers only the lowest-risk first batch:

- Paderborn MAT reading and window generation;
- bearing sensor-packet construction and serialization;
- packet-to-Paderborn source mapping.

H5 inference, cloud diagnosis models, storage-schema ownership, model training,
and global-analysis layout are separate future Stage 10 batches and are out of
scope here.

## Existing Baseline

- Stage 9 full regression: 781 passing tests.
- `BearingInputAdapterProvider` already isolates sender ingestion behind the
  scenario registry.
- The implementation still lives in `sender.mat_reader`, `sender.packet`, and
  `sender.source_mapping`.
- Those modules contain explicit Paderborn, bearing identifier, bearing signal,
  sampling-rate, packet-shape, and source-dataset knowledge.
- Existing production and tests import the old `sender.*` paths, so they must
  remain valid.
- The current worktree protection list remains:
  - deleted `cloud_edge_project/cloud_service/verification/conftest.py`;
  - deleted `cloud_edge_project/quick_demo.py`;
  - deleted `cloud_edge_project/scheduler/.gitkeep`;
  - untracked `docs/assets/`;
  - untracked `docs/挑战杯项目申报书.docx`.

## Scope

This batch will:

- make `scenarios.bearing.ingestion` the single implementation owner for the
  three ingestion responsibilities;
- add one V1.2 compatibility export module;
- retain the three old sender modules as explicit thin re-export shims;
- make `BearingInputAdapter` use scenario-local implementations;
- add identity, output-equivalence, database-equivalence, and architecture
  tests;
- move one module at a time and verify it before continuing.

This batch will not:

- change sender control flow, MQTT publishing, scheduler requests, retry,
  acknowledgement, timing, or network behavior;
- change any external packet field or old import name;
- change MAT parsing, signal selection, sampling rates, window boundaries,
  units, packet validation, ID construction, or serialization;
- change source-mapping table names, columns, SQL, dataset values, or historical
  database behavior;
- delete old modules or old import paths;
- move unrelated sender, edge, cloud, scheduler, storage, or model code.

## Considered Approaches

### Chosen: move implementation and retain two compatibility boundaries

Move the implementation into the bearing scenario, export it through
`compatibility.bearing_v12`, and keep the old sender modules as explicit
re-export shims. This produces one implementation source of truth, retains old
imports, and avoids direct `sender -> scenarios.bearing` imports.

### Rejected: copy implementation

Keeping complete copies in both packages would reduce immediate import churn
but create two sources of truth. Fixes and behavior could diverge silently.

### Rejected: scenario aliases over sender implementation

Leaving implementation in `sender.*` and adding scenario aliases would be the
smallest code diff but would not complete the physical ownership change
required by Stage 10.

## Target Structure

```text
cloud_edge_project/
├── scenarios/bearing/ingestion/
│   ├── __init__.py
│   ├── provider.py
│   ├── mat_reader.py
│   ├── packet.py
│   └── source_mapping.py
├── compatibility/bearing_v12/
│   └── ingestion_exports.py
└── sender_module/sender/
    ├── mat_reader.py
    ├── packet.py
    └── source_mapping.py
```

The three sender modules remain physical files for import compatibility, but
contain no business implementation.

## Dependency Direction

The approved compatibility path is:

```text
legacy sender import
    -> compatibility.bearing_v12.ingestion_exports
    -> scenarios.bearing.ingestion implementation
```

The active scenario path is:

```text
BearingInputAdapter
    -> scenarios.bearing.ingestion local implementation
```

Rules:

- sender compatibility modules must not directly import `scenarios.bearing`;
- the compatibility module may import the bearing scenario;
- the scenario implementation may depend on generic `sender.input_adapter`
  contracts but not the three legacy implementation shims;
- no wildcard import is allowed in compatibility modules or shims;
- every retained public name is listed explicitly in `__all__`.

This arrangement prevents implementation duplication and avoids a cycle:
scenario providers use local implementation files, while old sender paths only
resolve outward through compatibility exports.

## Migration Sequence

### Step 1: MAT reading

Move without logic changes:

- `MatDataError`;
- `SignalSeries`;
- `SignalWindow`;
- `MatRecord`;
- `SOURCE_TO_PACKET`;
- `PACKET_UNITS`;
- `TEMPERATURE_SOURCE`;
- `load_mat_record` and private parsing helpers.

Add old/new import identity tests and compare fixed MAT parsing/window outputs
before starting Step 2.

### Step 2: packet construction

Move without logic changes:

- `PacketValidationError`;
- packet signal and identifier constants;
- private validation functions;
- `build_sensor_packet`;
- `serialize_packet`.

Compare exact packet dictionaries, generated IDs, serialized bytes, and error
types/messages before starting Step 3.

### Step 3: source mapping

Move without logic changes:

- `PADERBORN_FILENAME`;
- `extract_paderborn_bearing_code`;
- `PacketSourceMappingStore`;
- the unchanged table DDL, upsert SQL, dataset name, and dataset version.

Use a historical-shape temporary database to compare reads, inserts, updates,
and repeated construction through both old and new imports.

### Step 4: provider cutover

Update `scenarios.bearing.ingestion.provider` to import the three local scenario
modules. It must no longer import `sender.mat_reader`, `sender.packet`, or
`sender.source_mapping`.

## Compatibility Contract

`compatibility.bearing_v12.ingestion_exports` explicitly imports and re-exports
the retained public names from the three scenario implementation modules.

Each legacy sender module explicitly imports its old public names from that
compatibility module and defines a matching `__all__`.

Required compatibility:

- old and new exception classes are the same object;
- old and new data classes are the same object;
- old and new public functions are the same object;
- old constants retain the same values and, where mutable compatibility
  requires it, the same object;
- existing `from sender.<module> import <name>` statements continue to work;
- old pickles that import classes through the sender module can still resolve
  their names through the shim;
- no compatibility module reimplements parsing, validation, serialization, or
  SQL behavior.

The implementation class `__module__` naturally reflects its new scenario
location. This is intentional physical ownership metadata, not a public packet
or database behavior change.

## Data and Error Equivalence

### MAT and windows

For identical fixed input, old and new paths must produce identical:

- source path;
- signal names;
- time and value arrays;
- sample rates;
- temperature arrays;
- window count and sequence numbers;
- time boundaries and sample indexes;
- channel sample counts, values, and units;
- temperature selection;
- `MatDataError` type and message.

### Packets

For identical fixed input, old and new paths must produce identical:

- packet dictionary;
- packet ID;
- all identifier and timestamp fields;
- signal dictionaries and ordering semantics;
- UTF-8 JSON bytes;
- `PacketValidationError` type and message.

### Source mapping

Old and new paths must preserve:

- `packet_source_mapping` table and columns;
- primary key and upsert behavior;
- Paderborn filename parsing;
- `dataset_name="paderborn"`;
- `dataset_version="paderborn_v1"`;
- bearing source code normalization;
- read result shape;
- historical rows and repeated initialization.

Any mismatch is a stop condition. Tests must not change expected outputs to
accommodate the move.

## Planned Files

New implementation files:

- `cloud_edge_project/scenarios/bearing/ingestion/mat_reader.py`;
- `cloud_edge_project/scenarios/bearing/ingestion/packet.py`;
- `cloud_edge_project/scenarios/bearing/ingestion/source_mapping.py`.

New compatibility file:

- `cloud_edge_project/compatibility/bearing_v12/ingestion_exports.py`.

Modified files:

- `cloud_edge_project/scenarios/bearing/ingestion/provider.py` for local imports;
- `cloud_edge_project/scenarios/bearing/ingestion/__init__.py` only if explicit
  public scenario exports are needed;
- `cloud_edge_project/sender_module/sender/mat_reader.py` as a shim;
- `cloud_edge_project/sender_module/sender/packet.py` as a shim;
- `cloud_edge_project/sender_module/sender/source_mapping.py` as a shim;
- focused tests and architecture guards.

No other production file is planned for modification. If another production
file requires a logic change, this batch stops and returns to design review.

## Verification

### Baseline

- record branch, HEAD, and the protected worktree list;
- run existing MAT reader, packet, source mapping, input-adapter, sender-flow,
  H5 probe, architecture, and compatibility tests;
- retain the 781-test full-suite baseline.

### Per-module gates

After each module move:

- run old-path tests;
- run new-path tests;
- assert public object identity;
- compare exact success outputs;
- compare exact error types and messages;
- run `git diff --check`.

The next module may not move until the current module passes.

### Integration and architecture

- build `BearingInputAdapter` through the scenario registry;
- run its complete prepare, window, packet, persistence, and serialization flow;
- verify the old sender imports still work;
- verify sender shims contain no bearing algorithm, SQL, or signal constants;
- verify sender modules have no direct `scenarios.bearing` import;
- verify the scenario provider has no dependency on the three sender shims;
- verify only the scenario implementation owns the business definitions.

### Full acceptance

- run the complete suite with the established formal model environment;
- require at least 781 baseline tests plus new tests;
- inspect the committed range for only this ingestion batch and its docs/tests;
- request an independent read-only review;
- fix every Critical and Important finding.

## Stop Conditions

Stop the batch and request direction if:

- sender controller, MQTT, scheduler, public packet shape, or external message
  behavior must change;
- old and new windows, packet bytes, validation errors, or database results
  differ;
- historical source-mapping data requires migration;
- retaining old imports requires duplicate implementation;
- another Stage 10 responsibility group must be rewritten simultaneously;
- an unrelated user modification overlaps a target file.

## Success Criteria

- The three business implementations exist only under
  `scenarios.bearing.ingestion`.
- `BearingInputAdapter` uses those local implementations.
- Old sender imports remain valid through the V1.2 compatibility module.
- Public classes, exceptions, functions, constants, outputs, errors, packet
  bytes, and database behavior remain equivalent.
- Sender code contains no direct bearing scenario import.
- No unrelated production code changes.
- Focused and full tests pass.
- Independent review reports no remaining Critical or Important issue.
- This first Stage 10 batch is reported complete without automatically starting
  the H5, cloud model, storage SQL, training, or global-analysis batches.
