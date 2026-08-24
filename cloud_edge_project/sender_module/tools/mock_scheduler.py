from __future__ import annotations

import argparse
import json
import re
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock


class SchedulerHandler(BaseHTTPRequestHandler):
    device_id_lock = Lock()
    device_id_base = "machine_01"
    next_device_number = 1
    allocated_device_ids: OrderedDict[str, tuple[str, str]] = OrderedDict()

    def do_POST(self) -> None:
        if self.path not in {"/scheduler/decide", "/scheduler/device-id/next"}:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path == "/scheduler/device-id/next":
                response = self._allocate_device_id(request)
            else:
                bearing_number = int(request["bearing_id"].rsplit("_", 1)[1])
                response = {
                    "device_id": request["device_id"],
                    "sender_id": request["sender_id"],
                    "task_id": request["task_id"],
                    "bearing_id": request["bearing_id"],
                    "target_topic": f"edge/edge_{(bearing_number - 1) % 2 + 1}/input",
                }
            encoded = json.dumps(response).encode("utf-8")
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            encoded = json.dumps({"error_code": "INVALID_REQUEST", "message": str(exc)}).encode("utf-8")
            self.send_response(400)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    @classmethod
    def _allocate_device_id(cls, request: object) -> dict[str, str]:
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        base_device_id = request.get("base_device_id")
        request_id = request.get("request_id")
        if not isinstance(base_device_id, str) or not isinstance(request_id, str):
            raise ValueError("base_device_id and request_id are required")
        match = re.fullmatch(r"(.*?)(\d+)", base_device_id.strip())
        if match is None or not request_id.strip() or len(request_id.strip()) > 128:
            raise ValueError("invalid device ID allocation request")
        prefix, suffix = match.groups()
        request_id = request_id.strip()
        with cls.device_id_lock:
            previous = cls.allocated_device_ids.get(request_id)
            if previous is not None:
                previous_base, previous_device_id = previous
                if previous_base != base_device_id.strip():
                    raise ValueError("request_id was already used with another base_device_id")
                cls.allocated_device_ids.move_to_end(request_id)
                return {"device_id": previous_device_id}
            if cls.device_id_base != base_device_id.strip():
                raise ValueError("base_device_id does not match mock Scheduler configuration")
            device_id = prefix + str(cls.next_device_number).zfill(len(suffix))
            cls.next_device_number += 1
            cls.allocated_device_ids[request_id] = (base_device_id.strip(), device_id)
            if len(cls.allocated_device_ids) > 4096:
                cls.allocated_device_ids.popitem(last=False)
        return {"device_id": device_id}

    def log_message(self, format: str, *args: object) -> None:
        print(f"scheduler: {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local scheduler test double")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8003)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), SchedulerHandler)
    print(f"mock scheduler listening on http://{args.host}:{args.port}/scheduler/decide")
    server.serve_forever()


if __name__ == "__main__":
    main()

