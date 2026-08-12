from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


EDGE_SRC = Path(__file__).resolve().parents[1] / "src"
if str(EDGE_SRC) not in sys.path:
    sys.path.insert(0, str(EDGE_SRC))

from edge_diagnosis.cli import diagnose_packet  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose one 50 ms PerceptionResult for integration only"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    args = parser.parse_args()
    perception = json.loads(args.input.read_text(encoding="utf-8"))
    result = diagnose_packet(perception, args.model, args.metadata)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
