# 前置上下文阈值实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将补传流程改为只请求前置 20 包，允许同一发送器跨任务补传，并在截止时间按连续前置 16 包阈值决定降级聚合资格。

**Architecture:** `RawContextCoordinator` 固定创建前 20、后 0 的请求；接收器只按请求、发送器和锚点匹配批次，不再比较任务。完整 20 包立即进入 `complete`，截止时间扫描器根据紧邻锚点的连续前置后缀把 16～19 包置为 `partial_context`，少于 16 包置为 `insufficient_context`。SQLite v6 迁移持久化阈值并允许后置计数为 0。

**Tech Stack:** Python 3、FastAPI、SQLite、`unittest`

## Global Constraints

- 只修改 `codex/raw-context-coordinator`，不得修改 `main`。
- `before_packet_count=20`，`after_packet_count=0`，`minimum_context_packet_count=16`。
- 阈值不包含触发包；16～19 包必须形成紧邻触发包的连续后缀。
- 截止时间前即使达到 16 包也保持 `pending_context`；20 包齐全立即 `complete`。
- 截止时间到达时，16～19 包转为 `partial_context`，少于 16 包转为 `insufficient_context`。
- `complete`、`partial_context`、`insufficient_context` 是不可逆终态。
- `complete` 和 `partial_context` 是一次性终态聚合资格状态；下游聚合模块消费 `review_id`，并负责聚合作业调用与幂等性。
- 本模块只持久化聚合资格，不入队或调用聚合；核心测试只验证资格状态，不声称覆盖实际聚合触发。
- `task_id` 只用于存储追溯，不参与请求匹配；`sender_id` 必须匹配。
- 边缘成功回执继续只包含 `request_id`、`batch_id`、`status`、`context_status`、`results`。
- 保持纳秒时间戳、采样校验、幂等、冲突和原始数据不覆盖行为不变。

---

### Task 1: 持久化并发送前置请求契约

**Files:**
- Modify: `cloud_edge_project/cloud_service/raw_context/coordinator.py`
- Modify: `cloud_edge_project/cloud_service/raw_context/contracts.py`
- Modify: `cloud_edge_project/cloud_service/storage/schema.py`
- Modify: `cloud_edge_project/cloud_service/storage/database.py`
- Modify: `cloud_edge_project/cloud_service/storage/raw_context_repository.py`
- Test: `cloud_edge_project/test_raw_context_ingestion.py`

**Interfaces:**
- Produces: `RawContextCoordinator.create_and_dispatch()` 创建 `before_packet_count=20`、`after_packet_count=0` 的持久化请求和边缘请求体。
- Produces: `validate_edge_context_response(..., before_packet_count: int, after_packet_count: int)` 按实际请求验证边缘可用数量。
- Produces: SQLite v6 允许后置计数为 0、保存 `minimum_context_packet_count`，并允许 `partial_context` 终态。

- [ ] **Step 1: 写请求计数红测**

先增加从现有 v5 数据库初始化到 v6 的迁移测试，确认旧请求和复核记录仍存在，并能创建后置计数为 0、阈值为 16 的新请求。再将协调器测试中的预期请求改为：

```python
self.assertEqual(sent["before_packet_count"], 20)
self.assertEqual(sent["after_packet_count"], 0)
self.assertEqual(stored["before_packet_count"], 20)
self.assertEqual(stored["after_packet_count"], 0)
```

同步把 `create_review()` 等测试工厂及所有直接调用 `create_or_get()` 的地方改为 `before_packet_count=20`、`after_packet_count=0`、`minimum_context_packet_count=16`，避免测试仍构造旧契约。

边缘假响应的 `before_context.expected_count` 改为 20，`after_context.expected_count` 和 `available_count` 改为 0。

- [ ] **Step 2: 运行红测**

Run:

```powershell
python -m unittest test_raw_context_ingestion.RawContextCoordinatorTests -v
```

Expected: FAIL，因为当前表不允许后置计数为 0、没有阈值列和部分状态；协调器仍发送前 10、后 10，边缘响应校验仍固定要求两个方向都是 10。

- [ ] **Step 3: 先实现 v6 表结构与幂等迁移**

将 `SCHEMA_VERSION` 改为 6。新表约束包含：

```sql
before_packet_count INTEGER NOT NULL CHECK (before_packet_count > 0),
after_packet_count INTEGER NOT NULL CHECK (after_packet_count >= 0),
minimum_context_packet_count INTEGER NOT NULL
    CHECK (minimum_context_packet_count > 0),
request_status TEXT NOT NULL CHECK (
    request_status IN (
        'created', 'dispatched', 'pending_context',
        'complete', 'partial_context',
        'insufficient_context', 'dispatch_failed'
    )
)
```

`cloud_review.context_status` 同时增加 `partial_context`。

在 `database.py` 增加幂等 v5→v6 迁移。迁移开始前保存并关闭外键检查，启用 `legacy_alter_table`，重建 `cloud_review` 和 `raw_context_request` 后复制原数据；旧请求的 `minimum_context_packet_count` 填入 `MIN(before_packet_count + after_packet_count, 16)`。重建完成后恢复索引、关闭 `legacy_alter_table`、重新启用外键，并执行 `PRAGMA foreign_key_check`。迁移必须在最终 `DDL` 之前运行，异常时不得删除旧表。

扩展 `RawContextRequestRepository.create_or_get()` 签名和 INSERT：

```python
minimum_context_packet_count: int,
```

- [ ] **Step 4: 修改协调器与响应校验**

在协调器中改为：

```python
before_packet_count=20,
after_packet_count=0,
minimum_context_packet_count=16,
```

扩展响应校验签名：

```python
def validate_edge_context_response(
    payload: Any,
    *,
    request_id: str,
    anchor_packet_id: str,
    before_packet_count: int,
    after_packet_count: int,
) -> dict[str, Any]:
```

分别用 `before_packet_count` 和 `after_packet_count` 校验 `expected_count`，允许后置期望值和可用值为 0。

- [ ] **Step 5: 运行迁移与协调器测试**

Run: `python -m unittest test_raw_context_ingestion.RawContextRepositoryTests test_raw_context_ingestion.RawContextCoordinatorTests -v`

Expected: PASS。

- [ ] **Step 6: 提交**

```text
feat: persist preceding raw context request
```

---

### Task 2: 允许跨任务并拒绝后置批次

**Files:**
- Modify: `cloud_edge_project/cloud_service/raw_context/receiver.py`
- Modify: `cloud_edge_project/cloud_service/raw_context/contracts.py`
- Test: `cloud_edge_project/test_raw_context_ingestion.py`

**Interfaces:**
- Consumes: 数据库请求的 `before_packet_count=20`、`after_packet_count=0`。
- Produces: `_validate_request_match()` 只匹配 `sender_id` 和锚点字段；批次 `task_id` 可不同。
- Produces: 只接受 `context_position="before"`，允许的相对位置为 `-20..-1`。

- [ ] **Step 1: 写跨任务和方向校验红测**

增加：

```python
def test_accepts_batch_from_different_task_for_same_sender(self) -> None:
    payload = context_batch([100], position="before")
    payload["task_id"] = "task_00002"
    del payload["packets"][0]["task_id"]
    result = self.receiver.receive_batch(payload)
    self.assertEqual(result["results"][0]["status"], "accepted")

def test_rejects_after_batch_for_preceding_only_request(self) -> None:
    with self.assertRaises(ContractError) as caught:
        self.receiver.receive_batch(context_batch([102], position="after"))
    self.assertEqual(caught.exception.code, "INVALID_CONTEXT_BATCH")
```

保留并复跑不同 `sender_id` 返回 `CONTEXT_REQUEST_MISMATCH` 的测试。

- [ ] **Step 2: 运行红测**

Run:

```powershell
python -m unittest test_raw_context_ingestion.RawContextReceiverTests.test_accepts_batch_from_different_task_for_same_sender test_raw_context_ingestion.RawContextReceiverTests.test_rejects_after_batch_for_preceding_only_request -v
```

Expected: 第一个因任务不匹配失败，第二个未返回要求的错误码。

- [ ] **Step 3: 最小修改匹配和范围逻辑**

从 `_validate_request_match()` 的匹配字段中删除 `task_id`，保留：

```python
for field in ("sender_id", "anchor_packet_id", "anchor_sequence_number"):
```

当 `request["after_packet_count"] == 0` 且批次方向为 `after` 时抛出：

```python
raise ContractError(
    "INVALID_CONTEXT_BATCH",
    "after context is not requested",
)
```

将单包范围校验改为使用请求允许的前置范围，消除 `anchor - 10` 的硬编码，确保 `-20..-1` 合法而 `after` 不合法。若需要改变 `validate_raw_context_packet()` 签名，传入请求的 `before_packet_count` 和 `after_packet_count`，不得再次复制固定数值。

- [ ] **Step 4: 运行接收器相关测试**

Run: `python -m unittest test_raw_context_ingestion.RawContextReceiverTests -v`

Expected: PASS。

- [ ] **Step 5: 提交**

```text
fix: allow cross-task preceding context
```

---

### Task 3: 实现截止阈值与部分上下文状态

**Files:**
- Modify: `cloud_edge_project/cloud_service/storage/raw_context_repository.py`
- Modify: `cloud_edge_project/cloud_service/raw_context/contracts.py`
- Test: `cloud_edge_project/test_raw_context_ingestion.py`

**Interfaces:**
- Produces: `RawContextRequestRepository.mark_partial()`。
- Produces: `RawContextRequestRepository.expire_due()` 根据连续后缀转为 `partial_context` 或 `insufficient_context`。

- [ ] **Step 1: 写迁移和截止时间红测**

增加仓储测试：

```python
def test_expire_due_marks_sixteen_contiguous_packets_partial(self) -> None:
    # 插入相对位置 -16..-1
    expired = self.repository.expire_due(now_ns=self.deadline_at_ns + 1)
    self.assertEqual(expired, ["ctx_req_001"])
    self.assertEqual(self.repository.get("ctx_req_001")["request_status"], "partial_context")
    self.assertEqual(self.review_repository.get(self.review_id)["context_status"], "partial_context")

def test_expire_due_rejects_sixteen_packets_with_internal_gap(self) -> None:
    # 插入总计 16 个位置，但遗漏 -3，使紧邻锚点的连续后缀不足 16
    self.repository.expire_due(now_ns=self.deadline_at_ns + 1)
    self.assertEqual(self.repository.get("ctx_req_001")["request_status"], "insufficient_context")
```

- [ ] **Step 2: 运行红测**

Run: `python -m unittest test_raw_context_ingestion.RawContextRepositoryTests -v`

Expected: FAIL，因为截止扫描仍一律判定不足，且仓储没有 `mark_partial()`。

- [ ] **Step 3: 实现连续后缀和终态方法**

仓储中用相对位置计算紧邻锚点的连续包数：

```python
def _contiguous_before_count(positions: set[int], before_count: int) -> int:
    count = 0
    for position in range(-1, -before_count - 1, -1):
        if position not in positions:
            break
        count += 1
    return count
```

新增 `mark_partial()`，同步写入请求表和 `cloud_review.context_status`，保持 `cloud_review.review_status='preliminary'`。将 `partial_context` 加入所有终态保护集合。

`expire_due()` 对每个到期请求查询 `review_context_packets.relative_position`：

- 连续数量达到 `minimum_context_packet_count`：`partial_context`；
- 否则：`insufficient_context`，并把复核状态置为不足。

- [ ] **Step 4: 运行仓储测试**

Run: `python -m unittest test_raw_context_ingestion.RawContextRepositoryTests -v`

Expected: PASS。

- [ ] **Step 5: 提交**

```text
feat: persist partial raw context state
```

---

### Task 4: 调整接收状态机为“等满 20 或截止降级”

**Files:**
- Modify: `cloud_edge_project/cloud_service/raw_context/coordinator.py`
- Modify: `cloud_edge_project/cloud_service/raw_context/receiver.py`
- Modify: `cloud_edge_project/cloud_service/storage/raw_context_repository.py`
- Test: `cloud_edge_project/test_raw_context_ingestion.py`

**Interfaces:**
- Consumes: `minimum_context_packet_count` 和 `partial_context` 持久化能力。
- Produces: 20 个前置位置齐全时立即 `complete`；16～19 包在截止前保持 `pending_context`；到期后由仓储完成降级判定。

- [ ] **Step 1: 写状态机红测**

增加：

```python
def test_twenty_preceding_positions_complete_without_after_batch(self) -> None:
    self.receiver.receive_batch(context_batch(range(81, 91), position="before"))
    result = self.receiver.receive_batch(context_batch(range(91, 101), position="before"))
    self.assertEqual(result["context_status"], "complete")

def test_sixteen_positions_stay_pending_before_deadline(self) -> None:
    self.receiver.receive_batch(context_batch(range(85, 95), position="before"))
    result = self.receiver.receive_batch(context_batch(range(95, 101), position="before"))
    self.assertEqual(result["context_status"], "pending_context")
```

更新旧的“前后各 10 包完成”测试，删除后置上传预期。

- [ ] **Step 2: 运行红测**

Run:

```powershell
python -m unittest test_raw_context_ingestion.RawContextReceiverTests.test_twenty_preceding_positions_complete_without_after_batch test_raw_context_ingestion.RawContextReceiverTests.test_sixteen_positions_stay_pending_before_deadline -v
```

Expected: 20 个前置位置因当前期望集合仍含后置位置而不能完成。

- [ ] **Step 3: 最小修改接收完成判定**

`_is_complete()` 只比较：

```python
expected = set(range(-request["before_packet_count"], 0))
return self.reviews.context_positions(request["review_id"]) == expected
```

收到边缘 `insufficient_context` 或 `missing_sequence_numbers` 时，截止前不得直接调用 `mark_insufficient()`；保存 `last_error_code='EDGE_REPORTED_MISSING_CONTEXT'` 并保持 `pending_context`。完整 20 包仍优先转为 `complete`。

协调器收到边缘立即响应 `status="insufficient_context"` 时采用相同规则：保存原始边缘响应和缺失提示，但在截止时间前保持 `pending_context`，不得绕过 16 包阈值直接终止。

收到截止时间后的批次时，先调用 `expire_due(now_ns=received_at_ns)`，使已有 16～19 包正确落入 `partial_context`，然后返回 `CONTEXT_REQUEST_EXPIRED`；不得无条件覆盖为不足。

- [ ] **Step 4: 运行完整补传测试**

Run: `python -m unittest test_raw_context_ingestion -v`

Expected: PASS。

- [ ] **Step 5: 提交**

```text
feat: finalize context by preceding threshold
```

---

### Task 5: 同步正式方案文档并完成回归验证

**Files:**
- Modify: `D:\codex_workspace\Intelligent-Maintenance-Collaboration\local_word\云端模型职责分析\云端复核模块\数据库\边缘异常数据任务原始数据上云\异常数据补传接收与数据库写入方案.md`
- Modify only if tests require alignment: `cloud_edge_project/test_raw_context_ingestion.py`

**Interfaces:**
- Produces: 与设计规格和实现完全一致的正式方案说明、JSON 示例、状态机、数据库 DDL 和测试要求。

- [ ] **Step 1: 更新正式文档**

全文同步以下内容：

- 所有“前 10 + 后 10”改为“前 20、后 0”；
- 删除等待后置包、后置批次和前后双批次流程；
- 说明每批最多 10 包，因此前 20 包可分两个或更多 `before` 批次；
- 删除使用 `task_id` 进行请求匹配和连续性判断的规则；
- 增加 `partial_context`、16 包阈值、截止时间判定及不可逆终态；
- 明确 16～19 包必须形成 `-N..-1` 连续后缀；
- 更新请求、边缘响应、上传批次、云端回执、DDL、状态机、测试和完成标准；
- 保持 `review_id`、`context_ready` 为云端内部字段。

- [ ] **Step 2: 文档冲突扫描**

Run:

```powershell
rg -n "前 10|后 10|后置包|after.*10|必须.*20|task_id.*必须.*请求|context_ready.*返回" "D:\codex_workspace\Intelligent-Maintenance-Collaboration\local_word\云端模型职责分析\云端复核模块\数据库\边缘异常数据任务原始数据上云\异常数据补传接收与数据库写入方案.md"
```

Expected: 除明确说明“不再采用原前后方案”的历史对比外，不存在冲突描述。

- [ ] **Step 3: 运行特性分支全测**

Run:

```powershell
python -m unittest discover -v
python -m compileall -q cloud_service test_raw_context_ingestion.py
git diff --check
```

Expected: 全部通过。

- [ ] **Step 4: 运行原有主工作区云端回归**

Run:

```powershell
$env:PYTHONPATH='D:\codex_workspace\Intelligent-Maintenance-Collaboration\.worktrees\raw-context-coordinator\cloud_edge_project;D:\codex_workspace\Intelligent-Maintenance-Collaboration\cloud_edge_project'
python -m unittest discover -s 'D:\codex_workspace\Intelligent-Maintenance-Collaboration\cloud_edge_project\tests' -t 'D:\codex_workspace\Intelligent-Maintenance-Collaboration\cloud_edge_project' -v
```

Expected: 全部通过。

- [ ] **Step 5: 提交受版本控制的测试或文档调整**

正式方案位于被忽略的 `local_word`，不强制加入 Git。若本任务仅修改该文档，则不创建空提交；若包含跟踪测试调整，提交为：

```text
docs: align preceding context threshold
```
