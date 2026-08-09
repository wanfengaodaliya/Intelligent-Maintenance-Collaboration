# Scheduler Retry Consistency Design

## Goal

Keep the scheduler's one-request/one-node behavior while making retries and
internal state updates consistent. Only scheduler-owned code is in scope.
Sender and edge-service code remain unchanged.

## Existing Behavior That Must Remain

- One request describes one bearing and asks for one edge node.
- The scheduler ranks all eligible nodes and contacts only the current Top-1.
- A rejection, timeout, or invalid acknowledgement fails the current request;
  the scheduler never falls back to the second-ranked node inside that request.
- Edge nodes may process tasks concurrently. Process-local reservations remain
  part of capacity scoring.
- The request and response JSON contracts do not change.

## Retry Policy

Retries are identified by the same `task_id` being submitted again after the
previous scheduling attempt was marked `FAILED`.

### Explicit rejection

An edge response with `ack_status=REJECTED` is a definitive non-acceptance.
The current request fails. On a later retry of the same `task_id`, every node
that explicitly rejected that task is excluded, and the remaining eligible
nodes are ranked again. The new Top-1 is contacted. If no eligible untried node
remains, the retry fails with `NO_AVAILABLE_EDGE_NODE`.

### Ambiguous acknowledgement failure

Timeouts, network failures, malformed acknowledgements, and invalid
acknowledgements do not prove that the edge failed to create the task. The
current request fails. On a later retry of the same `task_id`, the scheduler
contacts only the node from that ambiguous attempt. This uses the edge
interface's existing `task_id` idempotency requirement and prevents the
scheduler from creating the same task on another node.

The retry remains a new HTTP request, but it is not allowed to change nodes
until the edge interface provides an explicit query or cancellation operation.

## Attempt Persistence

`assignment_attempt.ack_status` distinguishes outcomes needed by retry routing:

- `ACCEPTED`: the edge definitively accepted the task.
- `REJECTED`: the edge definitively rejected the task.
- `FAILED`: acknowledgement outcome is ambiguous or the scheduler failed
  locally.
- `PENDING`: the attempt has not reached a terminal local state.

The repository exposes the previous attempt information needed by the
scheduler. It does not add fields to the public sender/scheduler interface.

## Atomic Attempt Creation

`start_attempt` performs these operations in one `BEGIN IMMEDIATE` transaction:

1. Read the task's current status, owner, and attempt count.
2. Verify `assignment_status='SCHEDULING'` and `scheduling_owner=claim_id`.
3. Increment `attempt_count` with the same status/owner predicates.
4. Insert the corresponding `PENDING` attempt.

If ownership was lost, no counter update or attempt row is committed.

## Timestamp Protection

Node status and link snapshots may be at most five minutes ahead of the
scheduler's receive time. A larger future timestamp is rejected before registry
state is mutated:

- status report: `FUTURE_STATUS_REPORT`
- link snapshot: `FUTURE_LINK_SNAPSHOT`

The existing monotonic receive-time liveness rules remain unchanged. Rejecting
one bad future report must not prevent later valid reports from being accepted.

## Stability Score

The current implementation is retained: execution results from
`bearing_execution_result` and `task_assignment` are combined with `UNION ALL`,
globally ordered by `result_received_at_ns DESC`, and limited to 20 rows before
the completion ratio is calculated.

## Out of Scope

- Adding automatic status reporting to `edge_service`.
- Changing sender request generation.
- Adding an edge task query or cancellation endpoint.
- Multi-worker/shared reservation storage.
- Changing the fixed node-capacity contract or the meaning of `queue_length`.
- Changing request or response JSON fields.

## Verification

Tests must prove:

1. An explicit rejection fails the current request without fallback.
2. A later retry excludes all nodes that explicitly rejected that `task_id`.
3. An ambiguous ACK failure retries only the original node.
4. `start_attempt` cannot write after claim ownership is lost.
5. Far-future status and link timestamps are rejected without poisoning later
   valid updates.
6. The two result tables are globally ordered before selecting the latest 20.
7. Existing single-bearing, capacity-reservation, result-validation, and real
   edge-ingress contract tests continue to pass.
