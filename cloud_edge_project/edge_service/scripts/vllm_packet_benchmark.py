#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vLLM OpenAI兼容服务的单包定速吞吐压测。"""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
for _path in (_SRC, _REPO):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from model_service.output_validator import validate_model_output  # noqa: E402
from model_service.prompt import PROMPT_VERSION, build_messages  # noqa: E402
from model_input_contract import model_input_probe  # noqa: E402


@dataclass(frozen=True)
class RequestResult:
    request_id: str
    scheduled_at: float
    started_at: float
    completed_at: float
    http_ok: bool
    output_valid: bool
    error: str | None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def scheduling_delay_ms(self) -> float:
        return (self.started_at - self.scheduled_at) * 1000.0

    @property
    def http_latency_ms(self) -> float:
        return (self.completed_at - self.started_at) * 1000.0

    @property
    def scheduled_to_result_ms(self) -> float:
        return (self.completed_at - self.scheduled_at) * 1000.0


def _perception(index: int, sender_count: int) -> dict:
    sender_index = index % sender_count
    sequence_number = index // sender_count + 1
    result = copy.deepcopy(model_input_probe())
    result.update({
        "device_id": "device-benchmark",
        "bearing_id": "bearing-%02d" % sender_index,
        "sender_id": "sender-%02d" % sender_index,
        "task_id": "task-vllm-benchmark",
        "packet_id": "packet-%08d" % index,
        "sequence_number": sequence_number,
        "end_generate_timestamp_ns": 1_700_000_000_000_000_000 + index * 50_000_000,
        "feature_generated_at_ns": 1_700_000_000_010_000_000 + index * 50_000_000,
    })
    result["features"]["vibration"].update({
        "rms": 0.30 + (index % 7) * 0.01,
        "absolute_peak": 1.8 + (index % 5) * 0.1,
        "kurtosis": 3.0 + (index % 9) * 0.25,
    })
    return result


def _post_one(base_url: str, model: str, request_id: str, perception: dict,
              scheduled_at: float, request_timeout_s: float,
              max_tokens: int) -> RequestResult:
    wait_s = scheduled_at - time.perf_counter()
    if wait_s > 0:
        time.sleep(wait_s)
    started_at = time.perf_counter()
    body = {
        "model": model,
        "messages": build_messages(perception),
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Request-Id": request_id},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=request_timeout_s) as response:
            response_body = json.loads(response.read().decode("utf-8"))
        content = response_body["choices"][0]["message"]["content"]
        validation = validate_model_output(content)
        usage = response_body.get("usage") or {}
        completed_at = time.perf_counter()
        return RequestResult(
            request_id, scheduled_at, started_at, completed_at,
            True, validation["valid"],
            None if validation["valid"] else ",".join(validation["errors"]),
            usage.get("prompt_tokens"), usage.get("completion_tokens"),
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        error = "HTTP_%d:%s" % (exc.code, detail[:200])
    except Exception as exc:  # noqa: BLE001
        error = "%s:%s" % (type(exc).__name__, exc)
    return RequestResult(
        request_id, scheduled_at, started_at, time.perf_counter(),
        False, False, error,
    )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1,
                       math.ceil(percentile / 100.0 * len(ordered)) - 1))
    return round(ordered[index], 3)


def _latency_summary(values: list[float]) -> dict:
    return {
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "max": round(max(values), 3) if values else None,
    }


def run(args) -> dict:
    target_rate = args.rate_per_sender * args.senders
    request_count = max(1, math.ceil(target_rate * args.duration_s))

    for index in range(args.warmup_requests):
        now = time.perf_counter()
        result = _post_one(
            args.base_url,
            args.model,
            "warmup-%04d" % index,
            _perception(index, args.senders),
            now,
            args.request_timeout_ms / 1000.0,
            args.max_tokens,
        )
        if not result.output_valid:
            raise RuntimeError("warmup失败: %s" % result.error)

    benchmark_start = time.perf_counter() + 0.2
    futures = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        for index in range(request_count):
            scheduled_at = benchmark_start + index / target_rate
            futures.append(executor.submit(
                _post_one,
                args.base_url,
                args.model,
                "packet-request-%08d" % index,
                _perception(index, args.senders),
                scheduled_at,
                args.request_timeout_ms / 1000.0,
                args.max_tokens,
            ))
        results = [future.result() for future in as_completed(futures)]

    benchmark_end = max(result.completed_at for result in results)
    total_elapsed_s = benchmark_end - benchmark_start
    scheduling_delays = [result.scheduling_delay_ms for result in results]
    http_latencies = [result.http_latency_ms for result in results]
    end_to_end = [result.scheduled_to_result_ms for result in results]
    errors = Counter(result.error for result in results if result.error)
    sla_violations = sum(value > args.latency_sla_ms for value in end_to_end)
    unique_request_ids = len({result.request_id for result in results})
    all_valid = all(result.http_ok and result.output_valid for result in results)
    passed = all_valid and unique_request_ids == request_count and sla_violations == 0

    return {
        "status": "PASS" if passed else "FAIL",
        "model": args.model,
        "prompt_version": PROMPT_VERSION,
        "senders": args.senders,
        "rate_per_sender_req_s": args.rate_per_sender,
        "target_rate_req_s": target_rate,
        "duration_s": args.duration_s,
        "concurrency": args.concurrency,
        "scheduled_requests": request_count,
        "completed_requests": len(results),
        "unique_request_ids": unique_request_ids,
        "http_successes": sum(result.http_ok for result in results),
        "valid_outputs": sum(result.output_valid for result in results),
        "max_tokens": args.max_tokens,
        "average_prompt_tokens": round(
            sum(result.prompt_tokens or 0 for result in results) / len(results), 3
        ),
        "average_completion_tokens": round(
            sum(result.completion_tokens or 0 for result in results) / len(results), 3
        ),
        "sla_ms": args.latency_sla_ms,
        "sla_violations": sla_violations,
        "elapsed_until_all_completed_s": round(total_elapsed_s, 3),
        "completion_throughput_req_s": round(len(results) / total_elapsed_s, 3),
        "scheduling_delay_ms": _latency_summary(scheduling_delays),
        "http_latency_ms": _latency_summary(http_latencies),
        "scheduled_to_result_ms": _latency_summary(end_to_end),
        "errors": dict(errors),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8002")
    parser.add_argument("--model", default="edge-bearing-qwen")
    parser.add_argument("--senders", type=int, default=1)
    parser.add_argument("--rate-per-sender", type=float, default=20.0)
    parser.add_argument("--duration-s", type=float, default=10.0)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--warmup-requests", type=int, default=5)
    parser.add_argument("--request-timeout-ms", type=int, default=5000)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--latency-sla-ms", type=float, default=200.0)
    args = parser.parse_args()
    if args.senders < 1 or args.rate_per_sender <= 0 or args.duration_s <= 0 \
            or args.concurrency < 1 or args.warmup_requests < 0 or args.max_tokens < 1:
        parser.error("发送器、速率、时长和并发数必须合法")
    try:
        report = run(args)
    except Exception as exc:  # noqa: BLE001
        report = {"status": "BLOCKED", "error": "%s: %s" % (type(exc).__name__, exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
