# Stage 5 Scheduler Domain Generalization Design

## Goal

Make scheduler decision paths operate on scenario-neutral domain fields while
preserving every existing bearing API, database field, routing outcome, retry,
degradation rule, reason code, and final task status.

## Scope

This stage changes compatibility mapping and in-memory scheduler boundaries for
packet assignment, packet routing, device routing, deferred cloud/device work,
and repository adapters. It does not rename database tables or columns, migrate
data, rewrite scheduling algorithms, or change external bearing contracts.

## Chosen Approach

Use adapters at both sides of the scheduler domain:

1. Ingress adapters accept generic or legacy bearing fields and produce one
   canonical generic representation.
2. Routers and services use only the canonical representation where they make
   scheduling decisions.
3. Egress and repository adapters convert canonical fields back to the existing
   bearing V1.2 HTTP and database shapes.

This is preferred over carrying aliases through every function because aliases
would multiply branches and allow partial conversions. Direct schema or API
renaming is excluded because it would combine domain generalization with a
destructive migration.

## Compatibility Mapper

Add `compatibility/bearing_v12/scheduler_mapper.py` as the explicit translation
boundary for:

- `bearing_id` and `unit_id`;
- `bearing_results` and `unit_results`;
- `expected_bearing_count` and `expected_unit_count`;
- `bearing_result_ids` and `unit_result_ids`;
- `BEARING_EDGE_INFERENCE` and `edge_inference`.

Ingress accepts either generic fields or legacy bearing fields. If both forms
are present, their values and order must agree. Mismatches fail with a clear
validation error before scheduling. Legacy-only requests preserve current
validation errors where compatibility depends on them.

External legacy responses and calls remain bearing-shaped. Generic requests may
receive the same established response structure in this stage; defining a new
public response protocol is outside scope.

## Canonical Scheduler Domain

The canonical in-memory representation uses:

- `unit_id`;
- `unit_results`;
- `expected_unit_count`;
- `unit_result_ids`;
- generic capability identifiers such as `edge_inference`.

Scheduling decisions may inspect only required capability, confidence, network
state, node health, node load, delay budget, and cloud availability. They must
not inspect bearing fault labels or encode bearing-specific fault categories.

Small immutable domain values may be introduced where they materially reduce
dictionary aliasing. Existing algorithms are retained and receive equivalent
values through the canonical representation.

## Component Boundaries

### Assignment scheduler

Assignment request validation maps legacy `bearing_id` to `unit_id`. Ranking,
reservation, timeout, and retry code retain their current implementation. The
Edge control-request adapter maps generic `edge_inference` and `unit_id` back to
`BEARING_EDGE_INFERENCE`, `expected_bearing_ids`, and `assigned_bearings`.

### Packet routing and service

Packet routing consumes generic unit identity after ingress mapping. Decisions
and deferred-cloud instructions use the same confidence, node, link, and delay
values as before. Existing packet responses and cloud payloads are mapped back
to their legacy bearing fields.

### Device routing and service

Device summaries map `bearing_results`, counts, and result IDs into generic
unit fields before validation and routing. Conflict and arbitration semantics
are not generalized in this stage; only field names and dependency direction
change. Legacy device instructions and payloads remain unchanged.

### Deferred dispatchers

Deferred cloud and device services use the canonical fields where they pass
domain data between routers and delivery code. HTTP payloads sent to existing
Edge and Cloud endpoints remain bearing V1.2 compatible.

### Repositories

Repository public boundaries accept canonical scheduler values from services
and map them to the current columns and JSON keys immediately before SQL. Rows
are mapped back to canonical values when returned to scheduler services.

No table, column, index, migration, uniqueness constraint, or persisted JSON
shape is renamed in Stage 5.

## Data Flow

1. API receives a generic or bearing V1.2 request.
2. Compatibility mapper validates aliases and creates canonical generic fields.
3. Scheduler router/service makes the unchanged decision using generic data.
4. Repository adapter maps canonical values to existing bearing storage fields.
5. Delivery adapter maps canonical values to existing Edge/Cloud payloads.
6. API returns the established legacy-compatible response.

## Error Handling

Alias conflicts fail before state mutation. Existing scheduler errors, status
codes, reason codes, retry counters, timeout handling, and degradation paths are
preserved. Repository conversion failures remain validation failures and must
not trigger schema changes or silent field loss.

## Verification

Tests will prove:

- old bearing assignment, packet, and device requests remain valid;
- equivalent generic requests produce the same decisions;
- conflicting generic and legacy aliases are rejected;
- Edge control requests retain `BEARING_EDGE_INFERENCE` and legacy fields;
- Repository rows and database schemas remain unchanged;
- deferred cloud/device payloads remain unchanged;
- routing-matrix cases preserve selected nodes and cloud/edge choices;
- delay, retry, degradation, reason codes, and final task states match baseline;
- scheduler-specific, architecture, compatibility, and full project suites pass.

## Explicit Non-Goals

- No database migration or physical field rename.
- No scheduling-score, threshold, retry, timeout, or degradation rewrite.
- No bearing fault-label interpretation in scheduler logic.
- No consistency or arbitration rule extraction; that belongs to Stage 6.
- No new public response version.
- No cleanup or deletion of legacy database/API compatibility code.
