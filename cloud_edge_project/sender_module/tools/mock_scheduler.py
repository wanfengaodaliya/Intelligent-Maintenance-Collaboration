from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class SchedulerHandler(BaseHTTPRequestHandler):
    target_topic = "edge/edge_2/input"

    def do_POST(self) -> None:
        if self.path != "/scheduler/decide":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            response = {
                "task_id": request["task_id"],
                "sender_id": request["sender_id"],
                "target_topic": self.target_topic,
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

    def log_message(self, format: str, *args: object) -> None:
        print(f"scheduler: {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local scheduler test double")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--topic", default="edge/edge_2/input")
    args = parser.parse_args()
    SchedulerHandler.target_topic = args.topic
    server = ThreadingHTTPServer((args.host, args.port), SchedulerHandler)
    print(f"mock scheduler listening on http://{args.host}:{args.port}/scheduler/decide")
    server.serve_forever()


if __name__ == "__main__":
    main()

