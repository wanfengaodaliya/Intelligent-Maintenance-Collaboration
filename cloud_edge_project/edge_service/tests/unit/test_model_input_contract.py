# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import pytest

from model_input_contract import (
    ModelInputValidationError,
    model_input_probe,
    validate_model_input,
)
from model_service.prompt import PROMPT_VERSION, build_messages
from model_service.model_runner import ModelRunner


def test_complete_probe_matches_model_input_contract():
    validate_model_input(model_input_probe())


def test_prompt_contains_the_complete_input_without_projection():
    model_input = model_input_probe()
    messages = build_messages(model_input)
    user_content = messages[1]["content"]
    encoded = user_content.removeprefix("完整感知结果：\n").removesuffix("\n\n请输出诊断 JSON。")

    assert PROMPT_VERSION == "edge-model-prompt/1.1"
    assert json.loads(encoded) == model_input
    for feature_group in (
        "vibration",
        "phase_current_1",
        "phase_current_2",
        "current_relationship",
        "operating_context",
    ):
        assert feature_group in messages[0]["content"] or feature_group in encoded


def test_contract_rejects_non_finite_feature_values():
    model_input = model_input_probe()
    model_input["features"]["operating_context"]["bearing_module_temperature_c"] = float("nan")

    with pytest.raises(ModelInputValidationError, match="bearing_module_temperature_c"):
        validate_model_input(model_input)


def test_contract_rejects_unexpected_fields():
    model_input = model_input_probe()
    model_input["features"]["vibration"]["unrecognized"] = 1.0

    with pytest.raises(ModelInputValidationError, match="extra=unrecognized"):
        validate_model_input(model_input)


def test_model_runner_rejects_incomplete_input_before_runtime_access():
    runner = ModelRunner.__new__(ModelRunner)

    result = runner.infer({"features": {}}, request_id="request-invalid")

    assert result["valid"] is False
    assert result["error"] == "MODEL_INPUT_INVALID"
    assert result["request_id"] == "request-invalid"
