# Global Analysis Module Design

## Goal

Implement the bearing scenario's cloud global-analysis module as a read-only, historical analytics workflow.  It converts existing device, bearing, packet-review and arbitration results into `global_analysis_result/2.0`; it does not generate, correct, or backfill upstream results.

## Scope and boundaries

The module provides device health, bearing risk, packet-diagnosis performance, bearing-aggregation performance, arbitration performance, structured problem candidates, result persistence, and the existing POST/latest API endpoints.

It will not implement real-time diagnosis, edge aggregation, cloud review, arbitration, model training or update/distribution workflows.

## Architecture

`GlobalAnalysisService` validates the request, obtains normalized history from `GlobalAnalysisDataSource`, calls six pure analyzers, derives maintenance recommendations and problem candidates, persists the result, and returns it.  Analyzers accept `list[dict]` plus `GlobalAnalysisConfig`; they never access SQLite or mutate source rows.

The public layer contains the data-source protocol, immutable configuration, shared severity/rate helpers, device-health, packet-model, arbitration, problem-detection analyzers, orchestration service, and result repository.  The bearing layer contains SQLite/Fake sources plus bearing-risk and bearing-aggregation analyzers.

## Data contracts

The source returns five normalized collections:

- `device_tasks`: device/task ID, final state, confidence, conflict flag, arbitration ID and completion time.
- `bearing_tasks`: bearing task state plus edge/cloud state, confidence, result source, optional aggregation/review versions and trigger reason.
- `packet_review_pairs`: packet-level edge/cloud labels, confidences, versions, operating context and timestamp.
- `bearing_review_pairs`: cloud-reviewed bearing edge/cloud pairs and aggregation metadata.
- `arbitrations`: status plus optional final action, resolution method, dominant bearing and rule version.

The SQLite source reads only existing storage.  Current storage does not persist the cloud packet label returned by the packet-review API, so the source returns no packet-review pairs until that upstream contract is persisted.  Packet analysis therefore reports `not_available`/`insufficient_data` correctly; the pure analyzer is fully covered with Fake source data.

## Analysis behavior

- Device health reports counts, rates, overall and recent risk rates, trailing abnormal run, and a two-window health trend.
- Bearing risk groups dynamically by bearing ID, reports current/recent risk and trends, identifies the primary-risk bearing, and detects multi-bearing degradation.
- Packet and bearing-pair analyzers report agreement, correction, under/over-estimation, version grouping, and optional condition/trigger breakdowns.  Their rates are explicitly scoped to cloud-reviewed records.
- Arbitration reports conflict rate from all device rows and success rate from arbitration rows, with the configured competition targets.
- Missing optional inputs produce `not_available`; absent or undersized required samples produce `insufficient_data`; rate values are `null` where a denominator is unavailable.
- The problem detector consumes only analyzer outputs.  It applies configurable warning thresholds and labels a first observation `unknown`, later recurring issues `persistent`, otherwise `temporary`.

## Persistence and compatibility

Results use `global_analysis_result/2.0` and are stored in `result_json`.  Existing query columns retain the task count, health trend, reviewed packet count, correction rate, conflict rate, arbitration success rate and creation time so current model-update lookup remains compatible.  The two existing HTTP endpoints remain unchanged.

## Testing and acceptance

Tests use a Fake source and direct analyzer inputs.  They cover valid metrics, data-insufficiency/null semantics, severity direction, model-version splits, condition buckets, trigger availability, arbitration targets, persistent candidates, SQLite loading behavior, persistence/latest lookup, and API response shape.  All core statistical results remain deterministic and do not use an LLM.
