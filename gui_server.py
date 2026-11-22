"""
gui_server.py - serwer HTTP GUI dla odwolania2json.

Trzy sekcje:
  /         → index.html  (Przesyłanie | Przegląd | Konfiguracja YAML)
  POST /api/process       → prześlij plik(i) PDF, przetwórz w pamięci, zwróć {session_id, files:[{name,json,provenance,text}]}
  GET  /api/session/<id>  → pobierz dane sesji
  GET  /api/yaml/<name>   → odczytaj plik YAML z definicjami
  PUT  /api/yaml/<name>   → zapisz plik YAML z definicjami
  GET  /pdf/<session>/<name> → udostępnij oryginalne bajty PDF dla podglądu w przeglądarce

Serwer przechowuje dane sesji w pamięci przez cały czas trwania procesu.
"""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFINITIONS_DIR = ROOT / "definitions"
EXPORTER_FILE = ROOT / "pipeline" / "json-exporter" / "exporter.py"
GUI_DIR = ROOT / "gui"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("gui_server")

# ─── lazy-load exporter ───────────────────────────────────────────────────────

_exporter_mod = None
_exporter_instance = None
_exporter_lock = threading.Lock()


def _load_exporter_module():
    global _exporter_mod
    if _exporter_mod is None:
        spec = importlib.util.spec_from_file_location("json_exporter", EXPORTER_FILE)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _exporter_mod = mod
    return _exporter_mod


def get_exporter():
    global _exporter_instance
    with _exporter_lock:
        if _exporter_instance is None:
            mod = _load_exporter_module()
            _exporter_instance = mod.JsonExporter()
        return _exporter_instance

# przechowywanie sesji w pamięci

_sessions: dict[str, dict] = {}  # session_id → {created, files: [...]}
_sessions_lock = threading.Lock()

ALLOWED_YAML_FILES = {
    "documents.yaml",
    "field_definitions.yaml",
    "formats.yaml",
    "lookups.yaml",
    "derivations.yaml",
}

# przetwarzanie

def process_pdf_bytes(name: str, data: bytes) -> dict:
    """Przetwarza przekazany plik PDF. Zwraca {name, label, json, provenance, text, error}."""
    result = {"name": name, "label": None, "json": {}, "provenance": {}, "text": "", "error": None}
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            exporter = get_exporter()
            prov: dict = {}
            label, json_data, report = exporter.process(tmp_path, provenance=prov)
            result["label"] = label
            result["json"] = json_data
            result["text"] = prov.pop("__text__", "")
            result["provenance"] = prov
            result["report"] = report.to_dict()
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass
    except Exception as exc:
        logger.exception("Error processing %s", name)
        result["error"] = str(exc)
    return result

# Helper dla typów mime

def _mime(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower()
    return {
        "html": "text/html; charset=utf-8",
        "js":   "application/javascript; charset=utf-8",
        "css":  "text/css; charset=utf-8",
        "json": "application/json",
        "yaml": "text/yaml; charset=utf-8",
        "pdf":  "application/pdf",
        "ico":  "image/x-icon",
    }.get(ext, "application/octet-stream")


def _multipart_parts(body: bytes, content_type: str) -> list[dict]:
    """Bardzo prosty parser multipart/form-data → lista {name, filename, data}."""
    m = re.search(r'boundary=([^\s;]+)', content_type)
    if not m:
        return []
    boundary = ("--" + m.group(1)).encode()
    parts = []
    for chunk in body.split(boundary)[1:]:
        if chunk.strip() in (b"", b"--", b"--\r\n"):
            continue
        sep = chunk.find(b"\r\n\r\n")
        if sep == -1:
            continue
        header_raw = chunk[:sep].decode("latin-1")
        body_part = chunk[sep + 4:]
        if body_part.endswith(b"\r\n"):
            body_part = body_part[:-2]

        cd = {}
        for line in header_raw.splitlines():
            if line.lower().startswith("content-disposition:"):
                for item in line.split(";"):
                    item = item.strip()
                    kv = re.match(r'(\w+)="([^"]*)"', item)
                    if kv:
                        cd[kv.group(1)] = kv.group(2)
        parts.append({"name": cd.get("name", ""), "filename": cd.get("filename", ""), "data": body_part})
    return parts


# przetwarzanie zapytań

class GUIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        logger.debug("HTTP %s", fmt % args)

    # routing

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path in ("/", "/index.html"):
            self._serve_file(GUI_DIR / "index.html")
        elif path.startswith("/api/session/"):
            self._api_get_session(path[len("/api/session/"):])
        elif path.startswith("/api/yaml/"):
            self._api_get_yaml(path[len("/api/yaml/"):])
        elif path.startswith("/api/pdf/"):
            self._api_get_pdf(path[len("/api/pdf/"):])
        elif path.startswith("/api/yaml_list"):
            self._api_yaml_list()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/process":
            self._api_process()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/yaml/"):
            self._api_put_yaml(path[len("/api/yaml/"):])
        else:
            self._send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # helpery

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path):
        if not path.exists():
            self._send_json({"error": f"file not found: {path}"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", _mime(str(path)))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # endpointy API

    def _api_process(self):
        ct = self.headers.get("Content-Type", "")
        body = self._read_body()
        parts = _multipart_parts(body, ct)

        pdf_parts = [p for p in parts if p["filename"].lower().endswith(".pdf")]
        if not pdf_parts:
            self._send_json({"error": "No PDF files in request"}, 400)
            return

        session_id = str(uuid.uuid4())
        files = []
        raw_pdfs = {}  # name → bytes, stored for in-session PDF serving

        for p in pdf_parts:
            name = p["filename"] or "document.pdf"
            logger.info("Processing %s (%d bytes)", name, len(p["data"]))
            result = process_pdf_bytes(name, p["data"])
            files.append(result)
            raw_pdfs[name] = p["data"]

        with _sessions_lock:
            _sessions[session_id] = {
                "created": time.time(),
                "files": files,
                "raw_pdfs": raw_pdfs,
            }

        # Zwracaj bez nieprzetworzonego pdfa
        self._send_json({"session_id": session_id, "files": files})

    def _api_get_session(self, session_id: str):
        with _sessions_lock:
            session = _sessions.get(session_id)
        if session is None:
            self._send_json({"error": "session not found"}, 404)
            return
        self._send_json({"session_id": session_id, "files": session["files"]})

    def _api_get_pdf(self, rest: str):
        """GET /api/pdf/<session_id>/<filename>"""
        parts = rest.split("/", 1)
        if len(parts) != 2:
            self._send_json({"error": "bad path"}, 400)
            return
        session_id, filename = parts[0], urllib.parse.unquote(parts[1])
        with _sessions_lock:
            session = _sessions.get(session_id)
        if session is None:
            self._send_json({"error": "session not found"}, 404)
            return
        pdf_bytes = session.get("raw_pdfs", {}).get(filename)
        if pdf_bytes is None:
            self._send_json({"error": "pdf not found"}, 404)
            return
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(pdf_bytes)))
        self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.end_headers()
        self.wfile.write(pdf_bytes)

    def _api_get_yaml(self, name: str):
        name = Path(name).name  # usuń resztę ścieżki
        if name not in ALLOWED_YAML_FILES:
            self._send_json({"error": "not allowed"}, 403)
            return
        f = DEFINITIONS_DIR / name
        if not f.exists():
            self._send_json({"error": "file not found"}, 404)
            return
        body = f.read_bytes()
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "text/yaml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_put_yaml(self, name: str):
        name = Path(name).name
        if name not in ALLOWED_YAML_FILES:
            self._send_json({"error": "not allowed"}, 403)
            return
        body = self._read_body()
        # Walidacja YAML przed zapisem
        try:
            import yaml
            yaml.safe_load(body)
        except Exception as exc:
            self._send_json({"error": f"invalid YAML: {exc}"}, 400)
            return
        f = DEFINITIONS_DIR / name
        f.write_bytes(body)
        # Zrzuć cache, żeby kolejny exporter widział nowe definicje
        global _exporter_instance
        with _exporter_lock:
            _exporter_instance = None
        self._send_json({"ok": True, "saved": name})

    def _api_yaml_list(self):
        files = []
        for name in sorted(ALLOWED_YAML_FILES):
            f = DEFINITIONS_DIR / name
            files.append({"name": name, "exists": f.exists()})
        self._send_json({"files": files})

# serwer

def serve(host="127.0.0.1", port=8765):
    GUI_DIR.mkdir(exist_ok=True)
    server = HTTPServer((host, port), GUIHandler)
    logger.info("GUI server running at http://%s:%d", host, port)
    logger.info("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    serve(args.host, args.port)
