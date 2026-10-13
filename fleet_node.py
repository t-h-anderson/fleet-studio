"""Fleet node: exposes this machine's agent sessions to a remote Fleet Studio.

Run one node per remote machine. Studio reaches it over HTTPS (--cert/--key) or
through an SSH tunnel to the default loopback bind. Every request needs the
bearer token from FLEET_NODE_TOKEN; Studio reads the same value from the
environment variable named in its host configuration. The node offers only the
backend operations Studio already performs locally: observe, catalogue, read
public activity, deliver a reviewed message, rename and open a session.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import uuid

from studio_agents import BACKENDS, ClaudeCodeBackend, CodexBackend, tmux_launch

MAX_BODY = 200_000


class NodeError(ValueError):
    pass


class Node:
    def __init__(self, data_root, codex_path=""):
        runtime = Path(data_root) / ".runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self.backends = {"codex": CodexBackend(codex_path), "claude-code": ClaudeCodeBackend(runtime=runtime)}

    def backend(self, payload):
        name = payload.get("backend")
        if name not in BACKENDS:
            raise NodeError("Unknown backend.")
        return self.backends[name]

    @staticmethod
    def session(payload):
        try:
            return str(uuid.UUID(str(payload.get("sessionId"))))
        except ValueError:
            raise NodeError("A valid session UUID is required.") from None

    @staticmethod
    def directory(payload, required=False):
        value = payload.get("directory") or ""
        if not isinstance(value, str) or (required and not value):
            raise NodeError("A folder is required.")
        if value and not Path(value).is_dir():
            raise NodeError("Folder does not exist on this node: " + value)
        return value

    def handle(self, action, payload):
        backend = self.backend(payload)
        if action == "catalogue":
            return {"sessions": backend.catalogue()}
        if action == "observe":
            ids = payload.get("sessionIds")
            if not isinstance(ids, list) or len(ids) > 500:
                raise NodeError("Provide at most 500 session IDs.")
            return backend.observe([self.session({"sessionId": sid}) for sid in ids])
        if action == "activity":
            return backend.activity(self.session(payload))
        if action == "send":
            text = payload.get("text")
            # Mirror Studio's control checks so a node never accepts slash or shell commands.
            if not isinstance(text, str) or not text.strip() or len(text) > 16000 or text.lstrip().startswith(("/", "!")):
                raise NodeError("Messages are plain instructions of at most 16000 characters.")
            status, detail = backend.send(self.session(payload), text, self.directory(payload))
            return {"status": status, "detail": detail}
        if action == "rename":
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 80:
                raise NodeError("A session name of at most 80 characters is required.")
            status, detail = backend.rename(self.session(payload), name)
            return {"status": status, "detail": detail}
        if action == "launch":
            sid, folder = self.session(payload), self.directory(payload, True)
            name, window = payload.get("name"), payload.get("window")
            if not isinstance(name, str) or not name or any(c in name for c in ';"\n\t') or not isinstance(window, str) or not window.replace("-", "").replace("_", "").isalnum():
                raise NodeError("Invalid agent or window name.")
            opened = tmux_launch(window, {"name": name, "directory": folder}, backend.resume_command(sid, folder))
            return {"opened": opened}
        raise NodeError("Unknown node action.")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logging.info("%s " + fmt, self.address_string(), *args)

    def reply(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        supplied = self.headers.get("Authorization", "")
        return secrets.compare_digest(supplied, "Bearer " + self.server.token)

    def do_GET(self):
        if self.path != "/node/health":
            return self.reply(404, {"error": "Not found."})
        if not self.authorized():
            return self.reply(401, {"error": "Unauthorized."})
        return self.reply(200, {"backends": list(BACKENDS)})

    def do_POST(self):
        if not self.authorized():
            return self.reply(401, {"error": "Unauthorized."})
        if not self.path.startswith("/node/"):
            return self.reply(404, {"error": "Not found."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise NodeError("Invalid request size.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise NodeError("Expected a JSON object.")
            return self.reply(200, self.server.node.handle(self.path[len("/node/"):], payload))
        except (NodeError, ValueError) as exc:
            return self.reply(400, {"error": str(exc)})
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            return self.reply(502, {"error": str(exc)[-1500:]})
        except Exception:
            logging.exception("Node request failed")
            return self.reply(500, {"error": "The node operation failed. See the node log."})


class NodeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, node, token):
        self.node, self.token = node, token
        super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bind", default="127.0.0.1", help="Address to listen on. Keep loopback unless TLS is configured.")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--data-root", type=Path, default=Path.home() / ".fleet-node")
    parser.add_argument("--codex-path", default="")
    parser.add_argument("--cert", type=Path, help="TLS certificate chain (PEM) for direct HTTPS access.")
    parser.add_argument("--key", type=Path, help="TLS private key (PEM).")
    args = parser.parse_args()
    token = os.environ.get("FLEET_NODE_TOKEN", "")
    if len(token) < 24:
        parser.exit(2, "Set FLEET_NODE_TOKEN to a random value of at least 24 characters.\n")
    loopback = args.bind in ("127.0.0.1", "localhost", "::1")
    if not loopback and not (args.cert and args.key):
        parser.exit(2, "Non-loopback binds require --cert and --key. Use an SSH tunnel otherwise.\n")
    args.data_root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=args.data_root / "node.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    server = NodeServer((args.bind, args.port), Node(args.data_root, args.codex_path), token)
    if args.cert and args.key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert, args.key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    scheme = "https" if args.cert else "http"
    print("Fleet node serving " + scheme + "://" + args.bind + ":" + str(server.server_port), file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
