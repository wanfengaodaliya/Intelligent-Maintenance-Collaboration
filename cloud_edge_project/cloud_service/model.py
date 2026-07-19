"""Compatibility facade for cloud inference callers."""

from cloud_service.mock_backend import CLOUD_NODE_ID
from cloud_service.service import infer_cloud


__all__ = ["CLOUD_NODE_ID", "infer_cloud"]

