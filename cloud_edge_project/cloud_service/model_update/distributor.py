"""Prepare cloud-side pending-delivery descriptions without edge-network calls."""

from __future__ import annotations

from typing import Any


def prepare_distribution(task: dict[str, Any], download_base_url: str) -> dict[str, dict[str, Any]]:
    url = f"{download_base_url.rstrip('/')}/cloud/model-update/{task['update_id']}/file"
    return {
        node: {
            "status": "pending_delivery",
            "update_type": task["update_type"],
            "old_version": task["old_version"],
            "new_version": task["new_version"],
            "download_url": url,
            "sha256": task["update_file_sha256"],
        }
        for node in task["target_edge_nodes"]
    }
