# -*- coding: utf-8 -*-
"""阶段 5 验证（方案 6.1）：三崩溃点恢复与死信治理。

崩溃点定义：
  A 提交前      —— 入队事务未完成，无记录，业务侧由上游 outbox 承担；
  B 发送前      —— 记录已领取（UPLOADING/PUBLISHING）但外部发送未发生；
  C 确认前      —— 外部发送已成功但本地确认未落库，重启后依赖下游幂等键收敛。
"""
from __future__ import annotations

import sqlite3

from core.diagnosis_contracts import (
    BearingDecisionResult,
    BearingLifecycleStatus,
    RoundClosureReason,
)
from device_decision import aggregate_device_round
from edge_runtime.device_result_outbox import DeviceResultOutbox
from raw_sample_capture import RawSampleFreezer, RawSampleRepository
from result_uploader import ResultUploader
from test_capacity_and_retention import _bearing


def _device_result(task_id: str = "task_001"):
    aggregate = aggregate_device_round(
        (_bearing("bearing_a"), _bearing("bearing_b")),
        expected_bearing_ids=("bearing_a", "bearing_b"),
        closure_reason=RoundClosureReason.ALL_BEARINGS_FINAL,
        closed_at_ns=10,
    )
    from dataclasses import replace

    return replace(aggregate, result_id=f"device_{task_id}_r1", task_id=task_id)


_NS = 1_000_000_000


# ---------- ResultUploader 三崩溃点 ----------


def test_uploader_crash_before_send_recovers_and_resends(tmp_path) -> None:
    """崩溃点B：领取（UPLOADING）后、发送前崩溃 → 重启恢复 PENDING 并重发。"""
    sent: list[str] = []
    uploader = ResultUploader(tmp_path / "edge.db", lambda path, payload: (sent.append(path), {"status": "accepted"})[1])
    uploader.enqueue_device(_device_result())

    # 模拟崩溃点B：手工置为领取中状态（发送未发生）。
    with sqlite3.connect(uploader.database_path) as connection:
        connection.execute("UPDATE v12_result_upload SET status='UPLOADING'")

    # 重启：新实例构造时恢复 UPLOADING→PENDING。
    rebooted = ResultUploader(tmp_path / "edge.db", lambda path, payload: {"status": "accepted"})
    assert rebooted.run_once(0) == 1
    assert rebooted.health()["ACKNOWLEDGED"] == 1


def test_uploader_crash_after_send_before_ack_converges_via_idempotency(tmp_path) -> None:
    """崩溃点C：下游已收到但确认未落库 → 重启重发 → duplicate → 收敛。"""
    calls: list[dict] = []

    def downstream(path, payload):
        calls.append(payload)
        # 下游以 result_id 幂等：首次 accepted，重复投递返回 duplicate。
        return {"status": "accepted" if len(calls) == 1 else "duplicate"}

    uploader = ResultUploader(tmp_path / "edge.db", downstream)
    uploader.enqueue_device(_device_result(task_id="task_c"))
    # 首次发送已到达下游（calls 有 1 条），但确认 UPDATE 前崩溃：状态遗留 UPLOADING。
    first = ResultUploader(tmp_path / "edge.db", downstream)
    with sqlite3.connect(first.database_path) as connection:
        connection.execute("UPDATE v12_result_upload SET status='UPLOADING'")
    # 手工触发首轮发送以构造“发送成功但未确认”现场。
    calls.append({"result_id": "device_task_c_r1"})

    rebooted = ResultUploader(tmp_path / "edge.db", downstream)
    assert rebooted.run_once(0) == 1
    health = rebooted.health()
    assert health["ACKNOWLEDGED"] == 1
    assert health["backlog"] == 0
    # 同一 result_id 至少投递两次，由下游幂等键收敛为单一业务结果。
    assert len({call["result_id"] for call in calls}) == 1


def test_uploader_dead_letter_after_max_attempts_and_manual_requeue(tmp_path) -> None:
    def failing(path, payload):
        raise RuntimeError("scheduler unreachable")

    uploader = ResultUploader(tmp_path / "edge.db", failing, max_attempts=3)
    uploader.enqueue_device(_device_result(task_id="task_dl"))

    now = 0
    for _ in range(2):
        uploader.run_once(now)
        now += 100 * _NS
    uploader.run_once(now)
    health = uploader.health()
    assert health["DEAD_LETTER"] == 1
    assert health["backlog"] == 0

    # 死信不再被领取。
    assert uploader.run_once(now + 1_000 * _NS) == 0

    # 人工恢复入口：清零 attempt 重新入队，下游恢复后可完成。
    assert uploader.requeue_dead_letter("device_task_dl_r1") is True
    fixed = ResultUploader(tmp_path / "edge.db", lambda path, payload: {"status": "accepted"})
    assert fixed.run_once(0) == 1
    assert fixed.health()["ACKNOWLEDGED"] == 1


# ---------- RawSampleRepository 三崩溃点 ----------


def _frozen_sample(task_id: str):
    from test_raw_sample_capture import _decision, _packet

    return RawSampleFreezer().freeze(
        _decision(), (_packet(1), _packet(2))
    )


def test_raw_sample_crash_before_send_recovers(tmp_path) -> None:
    """崩溃点B：UPLOADING 遗留 → 重启恢复 PENDING → 重新领取上传。"""
    repository = RawSampleRepository(tmp_path, max_upload_attempts=5)
    sample = _frozen_sample("task_001")
    repository.enqueue(sample)

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("UPDATE raw_sample_queue SET status='UPLOADING'")

    uploaded: list[bytes] = []

    def transport(metadata, payload):
        uploaded.append(payload)
        return {"status": "accepted"}

    rebooted = RawSampleRepository(tmp_path, max_upload_attempts=5)
    from raw_sample_capture import RawAnalysisSampleUploader

    outcome = RawAnalysisSampleUploader(rebooted, transport).run_once(
        now_ns=sample.created_at_ns + 1
    )
    assert outcome.acknowledged == 1
    assert rebooted.health()["ACKNOWLEDGED"] == 1
    assert uploaded  # 原始证据最终到达下游。


def test_raw_sample_crash_after_upload_before_ack_converges(tmp_path) -> None:
    """崩溃点C：上传已成功但 acknowledge 未执行 → 重启重传 → duplicate 收敛。"""
    repository = RawSampleRepository(tmp_path, max_upload_attempts=5)
    sample = _frozen_sample("task_002")
    repository.enqueue(sample)

    uploads: list[str] = []

    def transport(metadata, payload):
        uploads.append(metadata["sample_id"])
        # sample_id 幂等：重复上传返回 duplicate。
        return {"status": "duplicate" if len(uploads) > 1 else "accepted"}

    # 构造现场：上传成功（第 1 次）但确认前崩溃，状态遗留 UPLOADING。
    uploads.append(sample.sample_id)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("UPDATE raw_sample_queue SET status='UPLOADING'")

    rebooted = RawSampleRepository(tmp_path, max_upload_attempts=5)
    from raw_sample_capture import RawAnalysisSampleUploader

    outcome = RawAnalysisSampleUploader(rebooted, transport).run_once(
        now_ns=sample.created_at_ns + 1
    )
    assert outcome.acknowledged == 1
    assert uploads.count(sample.sample_id) == 2  # at-least-once + 下游幂等收敛。


def test_raw_sample_legacy_schema_migrates_to_dead_letter(tmp_path) -> None:
    """旧库 CHECK 不含 DEAD_LETTER：启动时自动迁移，数据保留。"""
    database_path = tmp_path / "raw_sample_queue.db"
    repository = RawSampleRepository(tmp_path, max_upload_attempts=5)
    sample = _frozen_sample("task_003")
    repository.enqueue(sample)
    # 手工把表降级回旧 schema（去掉 DEAD_LETTER 的 CHECK）。
    with sqlite3.connect(database_path) as connection:
        connection.execute("ALTER TABLE raw_sample_queue RENAME TO raw_sample_queue_legacy")
        connection.execute(
            """CREATE TABLE raw_sample_queue(
            sample_id TEXT PRIMARY KEY,
            payload_sha256 TEXT NOT NULL,
            payload_path TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('PENDING','UPLOADING','ACKNOWLEDGED','CONFLICT','EXPIRED')),
            attempt_count INTEGER NOT NULL,
            next_attempt_at_ns INTEGER,
            last_error TEXT,
            created_at_ns INTEGER NOT NULL
            )"""
        )
        connection.execute(
            """INSERT INTO raw_sample_queue
            SELECT sample_id,payload_sha256,payload_path,metadata_json,status,
                   attempt_count,next_attempt_at_ns,last_error,created_at_ns
            FROM raw_sample_queue_legacy"""
        )
        connection.execute("DROP TABLE raw_sample_queue_legacy")

    migrated = RawSampleRepository(tmp_path, max_upload_attempts=2)
    assert migrated.get(sample.sample_id) is not None  # 数据保留。

    def failing(metadata, payload):
        raise RuntimeError("cloud unreachable")

    from raw_sample_capture import RawAnalysisSampleUploader

    uploader = RawAnalysisSampleUploader(migrated, failing)
    for round_index in range(3):
        uploader.run_once(now_ns=sample.created_at_ns + round_index * 10_000 * _NS)
    health = migrated.health()
    assert health["DEAD_LETTER"] == 1
    assert health["backlog"] == 0

    assert migrated.requeue_dead_letter(sample.sample_id) is True
    assert migrated.health()["PENDING"] == 1


# ---------- DeviceResultOutbox 崩溃点C（发布确认前） ----------


def test_outbox_crash_after_publish_before_ack_republishes_at_least_once(tmp_path) -> None:
    """崩溃点C：MQTT 发布成功但 PUBLISHED 确认前崩溃 → 重启重发（at-least-once）。"""
    published: list[str] = []

    def publisher(payload):
        published.append(payload["result_id"])
        return None

    outbox = DeviceResultOutbox(tmp_path / "edge.db", publisher, max_attempts=5)
    result = _device_result(task_id="task_oc")
    outbox.enqueue(result)
    outbox.run_once(0)  # 第 1 次发布成功。

    # 构造现场：发布已到达 broker，但确认 UPDATE 前崩溃，状态遗留 PUBLISHING。
    with sqlite3.connect(outbox.database_path) as connection:
        connection.execute(
            "UPDATE device_result_outbox SET status='PUBLISHING', published_at_ns=NULL WHERE result_id=?",
            (result.result_id,),
        )

    rebooted = DeviceResultOutbox(tmp_path / "edge.db", publisher, max_attempts=5)
    assert rebooted.run_once(1) == 1
    health = rebooted.health()
    assert health["PUBLISHED"] == 1
    # 同一 result_id 发布两次：MQTT at-least-once 语义，下游以幂等键收敛。
    assert published.count(result.result_id) == 2
