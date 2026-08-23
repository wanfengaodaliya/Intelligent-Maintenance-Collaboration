# Stage 9 Reference Scenario Design

## Goal

Add a test-only minimal reference inspection plugin that proves a non-bearing
scenario can use the existing scenario registry, generic contracts,
consistency engine, arbitration engine, and storage-provider entry without
modifying production platform or bearing code.

The reference plugin is architecture evidence only. It is not a production
scenario and must not be described as a completed second application domain.

## Scope

Stage 9 will add a fixture package under:

```text
cloud_edge_project/tests/fixtures/scenarios/reference_inspection/
```

The plugin will provide exactly these six capabilities:

- `input_adapter`;
- `edge_inference`;
- `cloud_diagnosis`;
- `consistency_policy`;
- `arbitration_policy`;
- `storage_provider`.

It will deliberately omit model update, model management, global analysis,
training, and other optional capabilities. Tests will load it through a
test-owned `ScenarioRegistry`; production `bootstrap/scenarios.py` will not
register or import it.

Stage 9 will not start FastAPI or MQTT services. Protocol-level integration
tests will exercise the same registry, contracts, engines, and storage entry
used by those services without introducing a production test-injection path.

## Considered Approaches

### Chosen: independent fixture plugin package

Create a small, coherent plugin package plus contract, integration, and
architecture tests. This directly demonstrates the documented migration path:
adding a scenario-specific package and registering it without changing the
platform.

### Rejected: inline fake classes in one test

Inline fakes would be faster to write but would not provide convincing
evidence that a self-contained scenario plugin can be added without changing
core modules.

### Rejected: hidden production scenario

Placing the fixture under `scenarios/` and guarding it with an environment
variable would more closely resemble runtime assembly, but it would pollute
production configuration and contradict the test-only boundary.

## Architecture

The fixture package will contain:

```text
reference_inspection/
├── __init__.py
├── plugin.py
└── providers.py
```

`plugin.py` owns the manifest, capability bindings, and configuration
validation. It imports only generic core contracts and the fixture providers.

`providers.py` contains deterministic, test-only implementations:

- a fixed visual-inspection input adapter;
- an edge inference provider;
- a cloud diagnosis provider and handler;
- a simple consistency policy;
- a safety-first arbitration policy;
- an in-memory record store that also implements the existing storage schema
  registration protocol.

The plugin uses `scenario_id="reference_inspection"`. Its payloads use only
generic identifiers and conclusion fields. They must not contain bearing
identifiers, bearing labels, H5/MOMENT model knowledge, or imports from
`scenarios.bearing` or `compatibility.bearing_v12`.

No production module will import the fixture package. Tests create a fresh
registry, register the fixture instance, and resolve each capability through
the existing public registry methods.

## Data Flow

1. A test creates `ScenarioRegistry` and registers
   `ReferenceInspectionPlugin`.
2. The input provider builds an adapter that emits one deterministic request
   containing `scenario_id`, `task_id`, `unit_id`, `device_id`,
   `observation_window_id`, and generic evidence.
3. The resolved edge provider produces a deterministic generic diagnosis from
   a fixed defect score.
4. The resolved cloud handler reviews the same request and produces a
   deterministic generic diagnosis without relying on bearing compatibility.
5. The test maps the diagnoses into `ConsistencyRequest` and passes it to the
   unchanged `ConsistencyEngine` with the resolved reference policy.
6. If the reference result represents a safety defect, the unchanged
   `ArbitrationEngine` invokes the resolved reference rule and returns the
   deterministic `stop_and_inspect` action.
7. The resolved storage provider stores fixture records in memory and
   registers an idempotent test table through the existing
   `StorageProvider.initialize()` boundary. A temporary database test proves
   the generic initializer accepts the provider without modification.

All reference calculations are intentionally trivial and deterministic. They
exist to exercise dependency direction, not to model a real inspection
algorithm.

## Error Handling

- Unknown scenarios and missing capabilities keep the existing registry
  exceptions.
- The fixture rejects payloads with the wrong `scenario_id` or missing generic
  identity/evidence fields.
- It never supplies a default bearing scenario and never enters the bearing
  compatibility layer.
- Unsupported inspection states fail explicitly rather than silently mapping
  to a bearing or generic fallback state.
- Storage initialization is idempotent; repeated initialization neither
  duplicates records nor damages existing records.
- No production error code, fallback rule, retry policy, or API response is
  added or changed.

## Planned Files

New fixture files:

- `cloud_edge_project/tests/fixtures/scenarios/reference_inspection/__init__.py`;
- `cloud_edge_project/tests/fixtures/scenarios/reference_inspection/plugin.py`;
- `cloud_edge_project/tests/fixtures/scenarios/reference_inspection/providers.py`.

New tests:

- `cloud_edge_project/tests/contracts/test_reference_inspection_plugin.py`;
- `cloud_edge_project/tests/regression/test_reference_inspection_flow.py`;
- `cloud_edge_project/tests/architecture/test_reference_inspection_portability.py`.

Existing production files are not planned for modification. If implementation
inspection shows a production change is required, Stage 9 must stop and return
to design review instead of silently expanding scope.

## Verification

### Registry and optional capabilities

- Register and resolve all six declared providers.
- Confirm the manifest and binding sets are identical.
- Confirm omitted model-update and global-analysis capabilities raise the
  existing `MissingScenarioCapabilityError`.
- Confirm the fixture is absent from every production registry builder.

### Input, edge, and cloud diagnosis

- The fixed input contains only generic scenario fields.
- Edge and cloud providers return the exact expected state, confidence, risk,
  action level, model identity, version, and evidence.
- Results contain no bearing identifiers or bearing labels.
- Invalid or cross-scenario requests fail explicitly.

### Consistency and arbitration

- `ConsistencyEngine` runs the reference policy without modification.
- The expected unit, state, confidence, conflict, degradation, and action are
  deterministic.
- `ArbitrationEngine` invokes the reference safety rule and returns
  `stop_and_inspect` with the expected dominant unit and rule identifier.
- No bearing policy, label, risk mapping, or action mapping participates.

### Storage

- The existing generic storage initializer accepts the resolved reference
  provider alongside the current production storage provider.
- The reference schema can be initialized twice without error.
- A reference record remains readable after repeated initialization.
- Existing production tables and migrations remain unchanged.

### Architecture

- The fixture imports no bearing scenario or bearing compatibility module.
- `bootstrap`, `scheduler`, `edge_service`, `cloud_service`, and generic core
  contain no `reference_inspection` import or hard-coded behavior.
- Stage 9 changes are confined to the fixture package, its tests, and Stage 9
  design/plan documents.

### Regression

- Run the Stage 9 contract, integration, architecture, registry, engine, and
  storage tests first.
- Run `git diff --check`.
- Run the complete repository suite with the established formal model
  environment.
- The passing count must be at least the current 760-test baseline plus the
  new Stage 9 tests.
- Request an independent read-only review and fix every Critical or Important
  finding before acceptance.

## Explicit Non-Changes

Stage 9 will not modify:

- production bootstrap or service entry points;
- the bearing plugin or compatibility layer;
- scheduler, inference, consistency, or arbitration algorithms;
- existing database tables, columns, indexes, constraints, or migrations;
- models, weights, features, thresholds, training, or model lifecycle;
- public API, MQTT, HTTP, environment-variable, or startup behavior;
- the user's pre-existing deleted files or untracked documentation assets.

## Success Criteria

- The reference fixture registers and resolves exactly six optional scenario
  capabilities through the existing registry.
- Its fixed input completes edge diagnosis, cloud diagnosis, consistency,
  arbitration, and storage registration through existing generic boundaries.
- No production or bearing source file changes are needed.
- No reference-scenario vocabulary leaks into production modules.
- Focused tests, the full suite, and architecture checks pass.
- Independent review reports no remaining Critical or Important issues.
- Stage 9 is reported complete without automatically entering Stage 10.
