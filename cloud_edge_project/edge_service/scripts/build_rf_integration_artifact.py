from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


EDGE_SRC = Path(__file__).resolve().parents[1] / "src"
if str(EDGE_SRC) not in sys.path:
    sys.path.insert(0, str(EDGE_SRC))

from edge_diagnosis.integration_artifact import build_integration_artifact  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build an unqualified 50 ms RF artifact for edge integration"
    )
    parser.add_argument("--features", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--expected-features-sha256", required=True)
    parser.add_argument("--acknowledge-integration-only", action="store_true")
    args = parser.parse_args()
    report = build_integration_artifact(
        args.features,
        args.output_dir,
        acknowledge_integration_only=args.acknowledge_integration_only,
        expected_features_sha256=args.expected_features_sha256,
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
