# -*- coding: utf-8 -*-
from __future__ import annotations


_RESULT_SCORE = {"normal": 0, "warning": 1, "fault": 2}
_RISK_SCORE = {"low": 0, "medium": 1, "high": 2}


def action_level_for(edge_result: str, risk_level: str) -> int:
    try:
        return min(4, _RESULT_SCORE[edge_result] + _RISK_SCORE[risk_level])
    except KeyError as exc:
        raise ValueError("unsupported edge_result or risk_level") from exc
