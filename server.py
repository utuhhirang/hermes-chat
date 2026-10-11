#!/usr/bin/env python3
"""Web chat UI backend yang memakai OpenCode CLI sebagai otak chat.

Endpoint:
  GET  /                      -> index.html
  GET  /api/sessions          -> daftar session OpenCode (JSON)
  GET  /api/sessions/<id>     -> pesan session (user/assistant, teks saja)
  POST /api/chat              -> body {"session_id": str|null, "message": str}
                                 respons: NDJSON stream {"type":"session"|"text"|"done"|"error", ...}
"""
import json
import os
import subprocess
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPENCODE = os.environ.get("OPENCODE_BIN", "/root/.opencode/bin/opencode")
WORKDIR = os.environ.get("CHAT_WORKDIR", str(HERE))
MODEL = os.environ.get("CHAT_MODEL", "zrouter/claude-haiku-5.5")
PORT = int(os.environ.get("PORT", "8090"))


def run_opencode(args, timeout=600):
    return subprocess.run(
        [OPENCODE, *args], cwd=WORKDIR, capture_output=True, text=True, timeout=timeout
    )


def list_sessions():
    r = run_opencode(["session", "list", "--format", "json"], timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "session list gagal")
    data = json.loads(r.stdout or "[]")
    data.sort(key=lambda s: s.get("updated", 0), reverse=True)
    return [
        {"id": s["id"], "title": s.get("title") or "(tanpa judul)",
         "updated": s.get("updated"), "directory": s.get("directory")}
        for s in data
    ]


def get_session(session_id):
    if not session_id.startswith("ses_") or not session_id.replace("_", "").isalnum():
        raise ValueError("session id tidak valid")
    r = run_opencode(["export", session_id], timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "export gagal")
    raw = r.stdout[r.stdout.find("{"):]
    data = json.loads(raw)
    msgs = []
    for m in data.get("messages", []):
        text = "".join(p.get("text", "") for p in m.get("parts", []) if p.get("type") == "text")
        if text.strip():
            msgs.append({"role": m["info"]["role"], "text": text.strip(),
                         "created": m["info"].get("time", {}).get("created")})
    return {"id": session_id, "title": data.get("info", {}).get("title"), "messages": msgs}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[webchat] " + (fmt % args) + "\n")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            if path == "/api/sessions":
                return self._json(200, list_sessions())
            if path.startswith("/api/sessions/"):
                return self._json(200, get_session(path.rsplit("/", 1)[1]))
            self._json(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})

    def do_POST(self):
        if self.path != "/api/chat":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            return self._json(400, {"error": "body JSON tidak valid"})
        message = (req.get("message") or "").strip()
        session_id = req.get("session_id")
        if not message:
            return self._json(400, {"error": "message kosong"})

        cmd = [OPENCODE, "run", "--format", "json", "--model", MODEL]
        if session_id:
            cmd += ["-s", session_id]
        cmd.append(message)  # argv list, tanpa shell -> aman dari injeksi

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(obj):
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()

        proc = subprocess.Popen(cmd, cwd=WORKDIR, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        sid_sent = False
        try:
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = ev.get("sessionID")
                if sid and not sid_sent:
                    emit({"type": "session", "session_id": sid})
                    sid_sent = True
                if ev.get("type") == "text":
                    emit({"type": "text", "text": ev["part"].get("text", "")})
            err = proc.stderr.read()
            proc.wait()
            if proc.returncode != 0:
                emit({"type": "error", "error": err.strip() or f"exit {proc.returncode}"})
            emit({"type": "done"})
        except (BrokenPipeError, ConnectionResetError):
            proc.kill()


if __name__ == "__main__":
    print(f"webchat on http://127.0.0.1:{PORT}  (workdir={WORKDIR}, model={MODEL})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
