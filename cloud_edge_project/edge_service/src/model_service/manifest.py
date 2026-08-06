# -*- coding: utf-8 -*-
"""生成边缘模型清单（第 5 阶段）。

模型路径可通过环境变量 EDGE_MODEL_PATH 覆盖（不写死个人目录）。在 WSL
edge-bench venv 下运行，代码/提示词/模型服务稳定后再生成，避免后续修改导致
哈希失效。

用法（WSL）：
    EDGE_MODEL_PATH=/home/unic/models/Qwen2.5-1.5B-Instruct \
    python src/model_service/manifest.py --out var/model_manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1]  # src/ 目录
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from model_service.prompt import PROMPT_VERSION  # noqa: E402
from model_service.output_validator import OUTPUT_SCHEMA_VERSION  # noqa: E402

MANIFEST_SCHEMA_VERSION = "edge-model-manifest/1.0"
DEFAULT_MODEL_ID = "edge-bearing-qwen"
DEFAULT_MODEL_VERSION = "qwen2.5-1.5b-instruct/phase1"
DEFAULT_BASE_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
DEFAULT_MAX_TOKENS = 64


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_template_text() -> str:
    """取 prompt.py 中的系统提示词模板（与 build_prompt 内联的 system 文本一致）。"""
    return (
        "你是轴承设备状态诊断助手。把给定的感知结果转换为一个 JSON 诊断结论。\n"
        "严格要求：\n"
        "1. 只输出一个 JSON 对象，禁止输出 <think>、任何解释、分析或思考过程，禁止 Markdown 代码块。\n"
        "2. JSON 只包含以下三个字段，不允许增加或缺失：\n"
        '   edge_result: 只能是 "normal" | "warning" | "fault"\n'
        '   edge_risk_level: 只能是 "low" | "medium" | "high"\n'
        "   confidence: [0,1] 的浮点数，表示诊断分数\n"
        "3. 立即输出 JSON，不要有任何前言。"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="var/model_manifest.json")
    ap.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    ap.add_argument("--model-version", default=DEFAULT_MODEL_VERSION)
    ap.add_argument("--base-model", default=DEFAULT_BASE_MODEL)
    ap.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--model-path", default=None, help="覆盖环境变量 EDGE_MODEL_PATH")
    args = ap.parse_args()

    model_path = args.model_path or os.environ.get("EDGE_MODEL_PATH")
    if not model_path:
        print("必须提供模型路径：--model-path 或环境变量 EDGE_MODEL_PATH")
        sys.exit(1)
    model_dir = Path(model_path)

    import torch
    import transformers

    # 权重文件（safetensors 或分片）
    weights = {}
    for f in sorted(model_dir.glob("*.safetensors")):
        weights[f.name] = sha256_file(f)
    if not weights:
        print("未找到 *.safetensors 权重文件:", model_dir)
        sys.exit(1)

    tokenizer_files = {}
    for name in ("tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt"):
        p = model_dir / name
        if p.exists():
            tokenizer_files[name] = sha256_file(p)

    manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "model_id": args.model_id,
        "model_version": args.model_version,
        "model_role": "bearing_diagnosis_text_generation",
        "base_model_id": args.base_model,
        "base_model_revision": "unknown",
        "architecture": "Qwen2ForCausalLM",
        "artifact_format": "transformers_safetensors",
        "model_path": str(model_dir),
        "weights": weights,
        "tokenizer_files": tokenizer_files,
        "precision": "bfloat16",
        "quantization": "none",
        "max_new_tokens": args.max_new_tokens,
        "input_schema_version": "edge-model-input/1.0",
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": sha256_text(prompt_template_text()),
        "runtime": {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "cuda": torch.version.cuda if torch.cuda.is_available() else None,
            "device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        },
        "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "validation_result": "edge-model-validation/1.0",
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("manifest 已写入:", out.resolve())
    print("权重文件数:", len(weights), "| tokenizer 文件数:", len(tokenizer_files))


if __name__ == "__main__":
    main()
