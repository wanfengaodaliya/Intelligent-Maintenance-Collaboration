# Edge Raw Context Request Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the cloud-to-edge raw-context request match the edge-owned six-field contract exactly.

**Architecture:** Keep cloud-only coordination fields in `raw_context_request`. Build the outbound request from the stored request plus the immutable anchor packet timestamp read from `raw_packet_index`, so initial dispatch and retries use the same source of truth without a schema migration.

**Tech Stack:** Python 3, SQLite, `unittest`

## Global Constraints

- The outbound request contains exactly `request_id`, `sender_id`, `anchor_packet_id`, `anchor_end_generate_timestamp_ns`, `before_packet_count`, and `requested_at_ns`.
- `deadline_at_ns` remains cloud-internal with the existing three-second deadline.
- The received context-batch contract and aggregation eligibility rules do not change.
- Modify only the request coordinator, its core tests, and the raw-context design documents.

---

### Task 1: Lock the outbound request contract

**Files:**
- Modify: `cloud_edge_project/test_raw_context_ingestion.py`

**Interfaces:**
- Consumes: `RawContextCoordinator.create_and_dispatch(...)`
- Produces: a regression test asserting the complete request passed to `RawContextTransport.send(request)`

- [ ] **Step 1: Replace the old expected request with the six-field literal**

```python
self.assertEqual(transport.requests, [{
    "request_id": "ctx_req_001",
    "sender_id": "sender_01",
    "anchor_packet_id": "batch_000101",
    "anchor_end_generate_timestamp_ns": 5_050_000_000,
    "before_packet_count": 20,
    "requested_at_ns": 1_000_000_000,
}])
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m unittest test_raw_context_ingestion.RawContextCoordinatorTests.test_dispatches_exact_request_and_persists_pending_edge_response -v
```

Expected: failure because the implementation still emits four cloud-only fields and lacks `anchor_end_generate_timestamp_ns`.

### Task 2: Build the exact request from the persisted anchor packet

**Files:**
- Modify: `cloud_edge_project/cloud_service/raw_context/coordinator.py`
- Test: `cloud_edge_project/test_raw_context_ingestion.py`

**Interfaces:**
- Consumes: `RawPacketRepository.end_timestamp(*, sender_id: str, packet_id: str) -> int | None`
- Produces: `_request_payload(stored, anchor_end_generate_timestamp_ns)` with the exact six-field dictionary

- [ ] **Step 1: Give the coordinator access to the raw packet index**

```python
from cloud_service.storage.raw_packet_repository import RawPacketRepository

self.raw_packets = RawPacketRepository(database_path)
```

- [ ] **Step 2: Resolve the anchor timestamp before transport dispatch**

```python
anchor_end_generate_timestamp_ns = self.raw_packets.end_timestamp(
    sender_id=stored["sender_id"],
    packet_id=stored["anchor_packet_id"],
)
```

If the result is `None`, persist `dispatch_failed` with `ANCHOR_RAW_PACKET_NOT_FOUND` and do not call the transport.

- [ ] **Step 3: Replace the payload builder**

```python
def _request_payload(
    stored: dict[str, Any],
    anchor_end_generate_timestamp_ns: int,
) -> dict[str, object]:
    return {
        "request_id": stored["request_id"],
        "sender_id": stored["sender_id"],
        "anchor_packet_id": stored["anchor_packet_id"],
        "anchor_end_generate_timestamp_ns": anchor_end_generate_timestamp_ns,
        "before_packet_count": stored["before_packet_count"],
        "requested_at_ns": stored["requested_at_ns"],
    }
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```powershell
python -m unittest test_raw_context_ingestion.RawContextCoordinatorTests.test_dispatches_exact_request_and_persists_pending_edge_response -v
```

Expected: PASS.

- [ ] **Step 5: Add and run the missing-anchor regression**

Delete the anchor row from `raw_packet_index`, dispatch, and assert `dispatch_failed`, `ANCHOR_RAW_PACKET_NOT_FOUND`, and no transport request.

Run:

```powershell
python -m unittest test_raw_context_ingestion.RawContextCoordinatorTests -v
```

Expected: all coordinator tests pass.

### Task 3: Synchronize the module documentation

**Files:**
- Modify: `local_word/云端模型职责分析/云端复核模块/数据库/边缘异常数据任务原始数据上云/异常数据补传接收与数据库写入方案.md`

**Interfaces:**
- Consumes: the edge-owned six-field JSON contract
- Produces: an explicit separation between the edge-facing request and cloud-only persistence fields

- [ ] **Step 1: Replace the cloud-to-edge JSON example**

Document the exact six fields and `anchor_end_generate_timestamp_ns` semantics.

- [ ] **Step 2: Clarify cloud-only fields**

State that `task_id`, `anchor_sequence_number`, `after_packet_count`, `minimum_context_packet_count`, and `deadline_at_ns` remain internal and are not sent to the edge.

- [ ] **Step 3: Preserve the batch upload contract**

State that the edge still returns `anchor_sequence_number` in uploaded context batches after locating the trigger packet, because cloud ingestion uses it for relative-position validation.

### Task 4: Verify the scoped change

**Files:**
- Verify: `cloud_edge_project/test_raw_context_ingestion.py`
- Verify: `cloud_edge_project/cloud_service/raw_context/coordinator.py`

**Interfaces:**
- Consumes: all raw-context request and ingestion behaviors
- Produces: fresh test and syntax evidence

- [ ] **Step 1: Run the module suite**

```powershell
python -m unittest test_raw_context_ingestion -v
```

Expected: all tests pass.

- [ ] **Step 2: Compile the changed Python files**

```powershell
python -m compileall cloud_service/raw_context/coordinator.py test_raw_context_ingestion.py
```

Expected: exit code 0.

- [ ] **Step 3: Inspect the final diff**

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors and only scoped files changed.
