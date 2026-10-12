"""Web front end for the Paperleaf agent.

Stdlib only — no Flask, no build step. Serves a single page and proxies chat
turns to the same `run_turn` the CLI uses, so the UI is a view over the agent
rather than a second implementation of it.

    python3 server.py          # http://localhost:8000
    python3 server.py --port 9000
"""

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic

import agent
import data
import tools
from session import SessionState

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "static", "index.html")

_lock = threading.Lock()  # one conversation at a time; it's a demo


class Session:
    def __init__(self) -> None:
        self.state = SessionState()
        self.messages = []
        self.transcript = agent.Transcript(enabled=True, label="web ui")

    def close(self) -> None:
        self.transcript.close()


SESSION = None


def reset_session() -> None:
    global SESSION
    if SESSION is not None:
        SESSION.close()
    data.RETURNS.clear()  # so repeated demos don't hit the idempotency cache
    data.REPLACEMENTS.clear()  # same — replacements have their own idempotency cache
    data.HANDOFFS.clear()  # so repeated demos always land on the same agent/position
    data.reset_orders()  # drop any order create_replacement appended last run
    SESSION = Session()


def send(text: str) -> dict:
    """One user turn. Returns the reply plus everything the agent did."""
    events = []
    SESSION.transcript.user(text)
    SESSION.state.last_user_message = text
    SESSION.messages.append({"role": "user", "content": text})
    reply = agent.run_turn(
        CLIENT,
        SESSION.messages,
        SESSION.state,
        verbose=False,
        transcript=SESSION.transcript,
        last_user_message=text,
        on_event=events.append,
    )
    return {"reply": reply, "events": events}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter console
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(INDEX, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/api/reset":
            with _lock:
                reset_session()
            self._json({"ok": True})
            return

        if self.path == "/api/message":
            text = (payload.get("text") or "").strip()
            if not text:
                self._json({"error": "empty message"}, 400)
                return
            try:
                with _lock:
                    self._json(send(text))
            except anthropic.APIStatusError as exc:
                self._json({"error": str(exc)}, 502)
            except Exception as exc:
                self._json({"error": "{}: {}".format(type(exc).__name__, exc)}, 500)
            return

        self._json({"error": "not found"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Paperleaf agent web UI")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    agent.load_env_file()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "No API key found. Put ANTHROPIC_API_KEY=sk-ant-... in .env")

    global CLIENT
    CLIENT = anthropic.Anthropic()
    reset_session()

    print("Paperleaf agent UI  ->  http://localhost:{}".format(args.port))
    print("transcript: {}".format(SESSION.transcript.path))
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
