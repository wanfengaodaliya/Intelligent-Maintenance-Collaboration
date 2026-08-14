from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Portable edge service launcher")
    parser.add_argument(
        "--host",
        default=os.getenv("EDGE_SERVICE_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("EDGE_SERVICE_PORT", "8001")),
    )
    parser.add_argument(
        "--check-import",
        action="store_true",
        help="Validate edge application assembly without starting Uvicorn",
    )
    arguments = parser.parse_args(argv)

    from edge_service.app import app

    if arguments.check_import:
        return 0

    import uvicorn

    uvicorn.run(app, host=arguments.host, port=arguments.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
