# Stage 4 Sender Ingestion Decoupling Design

## Goal

Separate generic sender transport orchestration from bearing-specific input
processing without changing existing sender behavior or external contracts.

## Scope

Stage 4 changes only sender configuration compatibility, sender orchestration,
and bearing ingestion assembly. It does not generalize scheduler internals or
change MQTT publishing, retries, acknowledgements, network error handling, task
identifiers, packet identifiers, packet order, topics, or payload schemas.

## Chosen Approach

Add a thin `BearingInputAdapter` that reuses the existing MAT reader, packet
builder, and packet-source mapping implementation. The sender controller is
given an input adapter and remains responsible for task IDs, scheduler calls,
MQTT lifecycle, pacing, retries, acknowledgements, summaries, and errors.

This is preferred over injecting several unrelated callbacks because it gives
the scenario input flow one explicit boundary. It is preferred over moving all
sender files into the bearing plugin because that would expand the change into
transport behavior that must remain stable.

## Configuration Compatibility

New sender-node configuration accepts the generic `unit_id`. Existing bearing
configuration that supplies only `bearing_id` remains valid and maps that value
to `unit_id` through the bearing V1.2 compatibility layer.

If both fields are supplied, they must contain the same non-empty value. A
mismatch is rejected as a configuration error. Internally the sender uses
`unit_id`; `bearing_id` remains available as a compatibility view so existing
callers and output summaries do not change.

## Components

### Generic input contract

Define the smallest sender-facing input-adapter contract needed by the current
controller. It produces a prepared source with a first window for scheduling,
ordered windows for publication, packet construction, and source-proof
persistence. The contract does not expose MAT or bearing concepts.

### Bearing input adapter

`scenarios/bearing/ingestion/BearingInputAdapter` will:

- load Paderborn MAT data through the existing reader;
- generate the existing fixed-duration windows;
- validate bearing identifiers through the existing packet builder;
- build byte-for-byte compatible bearing packets;
- persist the existing Paderborn packet-source mapping.

No signal extraction, sampling, validation, packet-ID, or mapping algorithm is
rewritten.

### Bearing V1.2 compatibility

`compatibility/bearing_v12` owns the explicit `bearing_id`/`unit_id` mapping and
the compatibility view needed by legacy configuration and payload construction.
The external packet and scheduler request still use the field name
`bearing_id`.

### Sender controller

The controller obtains prepared scenario input and packets through the adapter.
It no longer imports the MAT reader, bearing packet builder, or bearing source
mapping store directly. The default production assembly uses
`BearingInputAdapter`; tests may inject a small fake adapter.

## Data Flow

1. Load configuration and resolve each node's generic `unit_id`.
2. The default bearing adapter opens the configured MAT source and creates the
   existing window iterator.
3. The adapter builds the same preview packet used to compute
   `packet_size_bytes`.
4. The controller sends the unchanged scheduler request.
5. For each sequence number, the adapter builds the same packet and saves the
   same source mapping.
6. The controller publishes the unchanged serialized packet to the unchanged
   assigned topic and retains the existing settlement and summary flow.

## Error Handling

Existing exception types and controller error paths remain in force. Adapter
errors propagate through the current sender-task failure handling. Empty MAT
records, early record termination, sequence mismatch, invalid bearing IDs, and
source-mapping failures retain their current messages and task outcomes where
covered by existing behavior.

## Verification

Tests will prove:

- old `bearing_id` configuration remains valid;
- new `unit_id` configuration is accepted;
- conflicting `bearing_id` and `unit_id` values fail clearly;
- fixed input produces identical packet dictionaries and serialized bytes;
- packet IDs and sequences remain unchanged for all 80 packets;
- scheduler request fields and values remain unchanged;
- MQTT topic selection, retries, PUBACK, and confirmation behavior remain
  unchanged;
- packet-source mappings retain their schema and values;
- sender-specific, architecture, and full project regression suites pass.

## Explicit Non-Goals

- No scheduler generalization.
- No MQTT publisher or scheduler-client refactor.
- No packet schema or topic change.
- No bulk movement of sender files into the bearing scenario.
- No modification of model, diagnosis, decision, consistency, arbitration, or
  storage behavior.
