# Stage 6 Consistency and Arbitration Decoupling Design

## Goal

Decouple device-level consistency evaluation and conflict arbitration from the
bearing scenario without changing the verified V1.2 behavior. Reuse the
existing generic cloud arbitration contracts and persistence instead of
rewriting the arbitration algorithm.

## Scope

Stage 6 will:

- add a scenario-neutral consistency engine and arbitration engine;
- place bearing comparison, state mapping, risk mapping, action mapping, and
  arbitration rules behind bearing providers;
- resolve consistency and arbitration capabilities through the scenario
  registry;
- retain all legacy V1.2 entry points and result shapes;
- add behavioral-equivalence and architecture tests.

Stage 6 will not:

- change conflict thresholds, action grades, risk mapping, rule priority, or
  weighted-fusion behavior;
- rewrite `edge_runtime/v12_flow.py` state transitions;
- change API paths, MQTT messages, database tables, columns, or indexes;
- move or delete the retained bearing implementation;
- change timeout, retry, correction, compensation, or audit behavior.

## Existing Baseline

- Full repository regression: 717 passing tests.
- Cloud arbitration already uses generic `DecisionUnit`,
  `ArbitrationContext`, and an injected `ScenarioArbitrationAdapter`.
- The remaining consistency coupling is concentrated in
  `edge_service/src/device_decision/aggregator.py`, which directly evaluates
  bearing lifecycle states, action grades, bearing IDs, and bearing conflict
  reasons.
- The bearing plugin declares consistency and arbitration capabilities but
  does not currently resolve executable providers for them.

## Chosen Approach

Use thin platform engines plus scenario policy adapters.

This approach preserves the existing algorithms while changing their
dependency direction. A wrapper-only approach would leave bearing behavior in
the platform path. A full unified decision-pipeline rewrite would alter the
state machine and persistence boundaries and is outside this stage.

## Architecture

### Consistency engine

`core/consistency_engine.py` defines scenario-neutral request, unit, and result
contracts plus `ConsistencyEngine`. The engine delegates evaluation to a
`ConsistencyPolicy`; it does not interpret labels, risk levels, action names,
or scenario-specific conflict rules.

`scenarios/bearing/decision/` provides `BearingConsistencyPolicy`. It retains
the current behavior:

- expected and received unit validation;
- closure status selection;
- maximum action-grade selection;
- minimum confidence and data-quality selection;
- action-grade-span conflict detection;
- bearing action and state mapping;
- the existing conflict reason code.

The legacy `aggregate_device_round()` entry remains available. Its V1.2
bearing objects are converted at the compatibility boundary, evaluated by the
generic engine, and converted back to the existing `DeviceDecisionResult`.

### Arbitration engine

`core/arbitration_engine.py` encapsulates the existing orchestration rule:

1. ask the scenario policy for a mandatory rule decision;
2. when no scenario rule triggers, call the retained weighted-fusion
   calculator;
3. return a generic decision for persistence and response assembly.

The engine is injected with the policy and fusion callable. It does not import
the bearing scenario or cloud persistence modules.

The existing cloud `DeviceArbitrationService` keeps responsibility for:

- conflict-id idempotency;
- arbitration IDs and timestamps;
- database persistence and audit data;
- scenario-result assembly;
- legacy response conversion.

The current `BearingDeviceArbitrationAdapter` remains the verified bearing
policy implementation. A stable provider export is added under
`scenarios/bearing/arbitration/`; the existing module path remains valid.

### Registry assembly

- `BearingScenarioPlugin` resolves `CONSISTENCY_POLICY` and
  `ARBITRATION_POLICY` when requested.
- the edge registry includes consistency capability;
- the cloud registry includes arbitration capability;
- providers are resolved once during service assembly, never per packet;
- unsupported or unresolved capabilities retain the established external
  error behavior.

## Data Flow

### Edge consistency flow

1. The existing V1.2 flow collects the current bearing revisions.
2. The compatibility mapper creates a generic consistency request.
3. `ConsistencyEngine` calls `BearingConsistencyPolicy`.
4. The compatibility mapper reconstructs `DeviceDecisionResult`.
5. Existing repositories, callbacks, outboxes, correction handling, and
   scheduler reporting continue unchanged.

### Cloud arbitration flow

1. The existing V1.2 API adapter validates and converts the old request.
2. The cloud scenario registry resolves `ARBITRATION_POLICY`.
3. `DeviceArbitrationService` invokes `ArbitrationEngine`.
4. The engine uses the existing bearing rule adapter and weighted fusion.
5. The service persists the same request/result JSON and returns the same
   response fields.
6. The V1.2 adapter reattaches the existing round and bearing result identity.

## Error Handling and Compatibility

- Existing validation codes, status codes, and legacy field vocabulary remain
  unchanged for legacy requests.
- `ArbitrationValidationError` remains the scenario validation error contract.
- Missing scenario capabilities continue to surface as
  `UNSUPPORTED_SCENARIO` at the existing API boundary.
- Duplicate conflict submissions continue returning the persisted result.
- No new database migration is introduced.
- Existing import paths remain supported; new providers are additive.

## Planned Files

New files:

- `cloud_edge_project/core/consistency_engine.py`
- `cloud_edge_project/core/arbitration_engine.py`
- `cloud_edge_project/scenarios/bearing/decision/__init__.py`
- `cloud_edge_project/scenarios/bearing/decision/provider.py`
- `cloud_edge_project/scenarios/bearing/arbitration/__init__.py`
- `cloud_edge_project/scenarios/bearing/arbitration/provider.py`
- `cloud_edge_project/compatibility/bearing_v12/decision_mapper.py`
- focused contract, scenario, architecture, and equivalence tests.

Existing files changed only where required:

- `cloud_edge_project/core/scenario_plugin.py` for precise policy protocols;
- `cloud_edge_project/scenarios/bearing/plugin.py` to resolve both providers;
- `cloud_edge_project/bootstrap/scenarios.py` to assemble edge/cloud policy
  capabilities;
- `cloud_edge_project/edge_service/src/device_decision/aggregator.py` to retain
  the legacy facade while delegating evaluation;
- `cloud_edge_project/cloud_service/device_arbitration/service.py` to delegate
  rule/fusion orchestration;
- `cloud_edge_project/cloud_service/app.py` to resolve arbitration through the
  registry.

If implementation inspection shows that a listed runtime file need not change,
it will be left untouched. No unrelated formatting or cleanup is permitted.

## Verification

Behavioral equivalence must cover:

- all-final, provisional, timeout-incomplete, and historical-correction device
  rounds;
- identical final state, action, action grade, confidence, quality, conflict
  flag, conflict reason, degraded flag, and real-time effect;
- scenario-rule arbitration, weighted fusion, manual review, and duplicate
  conflict idempotency;
- unchanged V1.2 request, response, status, and error contracts;
- registry resolution of both new providers;
- absence of bearing imports and bearing vocabulary in the two new core
  engines.

The focused tests must pass before the complete repository suite is run. The
stage is accepted only if the complete suite is at least the current 717-test
baseline plus the new tests, with no unexplained behavior change.

## Success Criteria

- The generic engines contain no bearing labels, mappings, or imports.
- Existing conflict decisions and arbitration results are byte-for-byte or
  field-for-field equivalent where timestamps/IDs are controlled.
- Existing databases and audit chains remain readable and writable.
- The bearing plugin exposes executable consistency and arbitration providers.
- Existing entry points and direct tests remain compatible.
- Full regression and architecture checks pass.

