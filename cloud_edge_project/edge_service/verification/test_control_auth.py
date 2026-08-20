from __future__ import annotations

import pytest

from common.control_auth import (
    ControlAuthError,
    ControlAuthVerifier,
    encode_control_json,
    sign_control_request,
)


SECRET = b"test-control-secret-that-is-at-least-32-bytes"


def _signed(
    path: str,
    body: bytes,
    *,
    timestamp: int = 1_000,
    nonce: str = "00112233445566778899aabbccddeeff",
) -> dict[str, str]:
    return sign_control_request(
        SECRET,
        method="POST",
        path=path,
        body=body,
        timestamp=timestamp,
        nonce=nonce,
    )


def test_verifier_accepts_once_and_rejects_replay() -> None:
    path = "/edge/tasks"
    body = encode_control_json({"task_id": "task-1"})
    verifier = ControlAuthVerifier(SECRET, clock=lambda: 1_000)
    headers = _signed(path, body)

    verifier.verify(
        method="POST", path=path, query_string="", body=body, headers=headers
    )
    with pytest.raises(ControlAuthError) as captured:
        verifier.verify(
            method="POST", path=path, query_string="", body=body, headers=headers
        )

    assert captured.value.code == "CONTROL_REPLAY_DETECTED"
    assert captured.value.status_code == 409


def test_future_dated_nonce_is_kept_for_its_entire_validity_window() -> None:
    now = [1_000.0]
    path = "/edge/tasks"
    body = encode_control_json({"task_id": "task-1"})
    verifier = ControlAuthVerifier(SECRET, clock=lambda: now[0])
    headers = _signed(path, body, timestamp=1_060)
    verifier.verify(
        method="POST", path=path, query_string="", body=body, headers=headers
    )

    now[0] = 1_061.0
    with pytest.raises(ControlAuthError) as captured:
        verifier.verify(
            method="POST", path=path, query_string="", body=body, headers=headers
        )

    assert captured.value.code == "CONTROL_REPLAY_DETECTED"


def test_verifier_rejects_tampering_query_and_expired_timestamp() -> None:
    path = "/edge/task-revocations"
    body = encode_control_json({"dispatch_id": "dispatch-1"})
    headers = _signed(path, body)

    for query, candidate_body, now, expected in (
        ("debug=1", body, 1_000, "CONTROL_TARGET_INVALID"),
        ("", body + b" ", 1_000, "CONTROL_AUTH_INVALID"),
        ("", body, 1_061, "CONTROL_TIMESTAMP_EXPIRED"),
    ):
        verifier = ControlAuthVerifier(SECRET, clock=lambda now=now: now)
        with pytest.raises(ControlAuthError) as captured:
            verifier.verify(
                method="POST",
                path=path,
                query_string=query,
                body=candidate_body,
                headers=headers,
            )
        assert captured.value.code == expected


def test_replay_cache_fails_closed_until_entries_expire() -> None:
    now = [1_000.0]
    verifier = ControlAuthVerifier(
        SECRET,
        clock=lambda: now[0],
        replay_cache_capacity=1,
    )
    path = "/edge/device-arbitration-results"
    body = encode_control_json({"result_id": "result-1"})
    verifier.verify(
        method="POST",
        path=path,
        query_string="",
        body=body,
        headers=_signed(path, body),
    )

    with pytest.raises(ControlAuthError) as captured:
        verifier.verify(
            method="POST",
            path=path,
            query_string="",
            body=body,
            headers=_signed(
                path,
                body,
                nonce="ffeeddccbbaa99887766554433221100",
            ),
        )
    assert captured.value.code == "CONTROL_REPLAY_CACHE_FULL"

    now[0] = 1_061.0
    verifier.verify(
        method="POST",
        path=path,
        query_string="",
        body=body,
        headers=_signed(
            path,
            body,
            timestamp=1_061,
            nonce="ffeeddccbbaa99887766554433221100",
        ),
    )
