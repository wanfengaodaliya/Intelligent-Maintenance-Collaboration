# Edge MQTT Ingress Design

**Date:** 2026-08-11

**Status:** Approved design, pending written-spec review

## Goal

Connect the formal SensorPacket stream published by `sender` to
`edge/edge_01/input` into `edge_service`. MQTT and HTTP ingestion must use one
packet-processing service, preserve the sender and scheduler task identity, and
retain the existing packet-routing and idempotency semantics.

## Current Contract

- The scheduler registers a task through `POST /edge/tasks` before returning an
  MQTT `target_topic` to the sender.
- The sender publishes QoS 1, non-retained JSON objects. A formal packet carries
  `device_id`, `sender_id`, `task_id`, `bearing_id`, `packet_id`,
  `sequence_number`, `end_generate_timestamp_ns`, and the 64 kHz/4 kHz signal
  set.
- `EdgeTaskIngress` binds each packet to the registered task and classifies an
  identical retransmission as `DUPLICATE`; reusing an identity with different
  content is a conflict.
- `PacketRoutingBridge` persists the raw packet and edge result, then calls the
  current `POST /scheduler/packet-route` contract. The scheduler owns the stable
  `decision_id` and downstream route idempotency.

## Confirmed Gaps

- `edge_service` does not subscribe to MQTT or manage an MQTT client lifecycle.
- The current `/edge/packets` endpoint passes the packet to the legacy
  `infer_edge` function. A real sender packet is rejected because that function
  expects the old `sensor_id`, start/end timestamp, and simplified 16 kHz data
  shape.
- `/edge/packets` treats `DUPLICATE` as an HTTP 400 instead of an idempotent
  replay.
- The default scheduler node is `edge_1` with `edge/edge_1/input`, while the
  running edge identity is `edge_01`.
- The root runtime requirements do not include `paho-mqtt`.

## Scope

This change includes MQTT subscription, shared packet processing, formal sender
packet support, identity-safe duplicate handling, lifecycle/configuration,
dependency wiring, and end-to-end tests.

It does not add device-level three-bearing arbitration, change scheduler routing
rules, invent production diagnosis thresholds, or migrate the source checkout's
old scheduler endpoints and cloud-transfer runtime.

## Architecture

### MQTT ingress

Add a focused MQTT ingress component adapted from the source runtime pattern:

- MQTT v3.1.1, QoS 1, persistent client session, exact subscription to
  `edge/edge_01/input`.
- A bounded queue with capacity 160 separates the Paho network callback from
  packet processing.
- JSON decoding accepts objects only. Invalid UTF-8/JSON is logged as a
  permanent rejection.
- Manual subscriber acknowledgements are sent only after the worker classifies
  an item as successfully handled, an exact duplicate, or a permanent poison
  message. Queue-full and transient processing failures are not acknowledged;
  the client reconnects so the broker can redeliver.
- The Paho client is injectable so deterministic tests exercise the real
  callback, queue, acknowledgement, and subscription code without requiring a
  machine-wide broker.

### Shared packet processor

Add `EdgePacketProcessor` as the single entry point used by both MQTT and
`POST /edge/packets`.

For a first delivery it performs:

1. `EdgeTaskIngress.receive_packet` for task binding and content fingerprinting.
2. `EdgeValidationCache` validation and raw packet retention.
3. `EdgePerception.downsample` and `EdgePerception.perceive` using the existing
   formal signal contract.
4. `EdgeModelPipeline` submission and completion wait, preserving its bounded
   queue, timeout, breaker, and fallback behavior.
5. Adapt the formal `EdgeResult` and processing timestamps to
   `PacketRoutingBridge` without changing any sender identity field.
6. Call `/scheduler/packet-route` and return the current route decision.

The local/mock profile uses the existing development test FIR, perception
configuration, and `edge_rule_test_v1` fallback. Test output remains explicitly
technical-only and does not claim diagnostic accuracy. A production profile
must provide approved production perception/model configuration; it must not
silently inherit development thresholds.

### Processing state and idempotency

The processor keeps an in-memory state per complete packet identity. This
matches the existing in-memory task ingress lifetime and adds no new durable
edge database.

- A concurrent identical delivery joins the existing processing state and does
  not enqueue a second inference.
- A duplicate after success returns the stored status and route decision.
- A duplicate after a transient scheduler failure reuses the cached inference
  result and retries only `PacketRoutingBridge`; it does not recompute features
  or inference.
- A conflicting payload remains `PACKET_CONTENT_CONFLICT` and never reaches
  inference or routing.
- After an edge process restart, the scheduler's durable assignment and stable
  packet decision remain authoritative; restoring edge task-registration state
  across restart is outside this change.

### HTTP behavior

`POST /edge/packets` becomes a thin adapter over `EdgePacketProcessor`:

- `202`: accepted or idempotent duplicate, including the stable packet identity
  and route decision when available.
- `400`: malformed packet, task/identity mismatch, invalid sequence, or other
  permanent validation error.
- `409`: same packet identity with conflicting content.
- `503`: transient model, queue, scheduler, or routing failure.

The existing legacy `infer_edge` endpoint remains available at `/edge/infer`;
it is no longer used for formal sender packets.

## Lifecycle and Configuration

- Add an `mqtt` section to `configs/local.yaml`: host `127.0.0.1`, port `1883`,
  keepalive 30 seconds, QoS 1, topic `edge/edge_01/input`, persistent client ID,
  and queue capacity 160.
- Add `paho-mqtt==2.1.0` to the root runtime requirements.
- Start the model pipeline and MQTT ingress from the FastAPI lifespan; stop and
  join both on shutdown.
- Initial broker unavailability must not prevent the HTTP service from becoming
  healthy. The MQTT client reports a degraded ingress state and retries with
  bounded reconnect backoff.
- `/health` reports MQTT configured topic and connection state without exposing
  credentials.
- Change the scheduler's default node/topic pair to
  `edge_01 -> edge/edge_01/input` so the default sender assignment matches the
  actual edge identity.

## Error Classification

Permanent MQTT acknowledgements include malformed JSON, invalid formal packet
shape, target/identity mismatch, invalid sequence, and content conflict. These
messages are logged and acknowledged to prevent an infinite poison-message
loop.

Transient failures include queue saturation, model timeout/unavailability,
scheduler timeout, and packet-route HTTP failure. They are not acknowledged on
the subscriber connection. Exact redelivery is safe because the processor and
scheduler both use stable packet identity and idempotent routing.

## Tests

### Deterministic end-to-end test

The required test builds a packet with the sender's real `build_sensor_packet`
and serialization contract, registers its task, and delivers the serialized
bytes through an injected Paho-compatible client. It exercises:

`sender serialization -> MQTT callback -> bounded queue -> shared processor ->
formal validation/perception/test inference -> PacketRoutingBridge -> current
packet-routing service`.

Assertions:

- subscription is exactly `edge/edge_01/input` with QoS 1;
- all six identity fields reaching packet routing equal the sender values;
- one valid packet yields one stable scheduler decision;
- retransmitting identical bytes yields the same outcome with one inference and
  one routing side effect;
- changing content under the same `packet_id` yields
  `PACKET_CONTENT_CONFLICT` with no second result;
- HTTP submission uses the same processor and produces the same identity and
  duplicate behavior.

### Optional live-broker test

An integration test marked `mqtt_live` runs only when a broker host/port is
provided. It uses real Paho publisher/subscriber traffic and verifies the same
single-packet and duplicate assertions. Its absence does not skip the mandatory
deterministic end-to-end test.

## Expected File Changes

- Create `edge_service/src/edge_mqtt_ingress.py`.
- Create `edge_service/src/edge_packet_processor.py`.
- Create focused unit/integration tests under `edge_service/tests/unit/` and
  `tests/`.
- Modify `edge_service/app.py` for construction, lifespan, HTTP adaptation, and
  health reporting.
- Modify `edge_service/src/packet_routing_bridge.py` to accept formal model
  output and explicit processing timestamps while preserving the legacy bridge
  call used by existing tests.
- Modify `configs/local.yaml`, `requirements.txt`, and
  `scheduler/node_registry.py` for runtime wiring.

## Acceptance Criteria

- A formal sender packet published to `edge/edge_01/input` reaches the existing
  packet routing service without schema translation to the legacy sensor shape.
- Task and packet identities are byte-for-byte unchanged across the ingress and
  route request.
- Exact QoS 1 redelivery produces no duplicate inference or routing side effect.
- Conflicting content is rejected deterministically.
- HTTP and MQTT share one implementation path.
- Focused tests and the deterministic end-to-end test pass with fresh output.
- Existing packet router, deferred cloud, task-ingress, and bridge tests remain
  passing.
