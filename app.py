import os
import sys
import json
import time
import shutil
import signal
import subprocess
import zipfile
import tempfile
import threading
import socket
from pathlib import Path
from datetime import datetime

from flask import Flask, request, jsonify, send_file, Response, abort, send_from_directory
from werkzeug.utils import secure_filename
import requests
import psutil
from dotenv import load_dotenv

# ─── Load Environment ─────────────────────────────────────────────────────────
load_dotenv()

BASE_DIR = Path(__file__).parent.resolve()
PROJECTS_DIR = BASE_DIR / "projects"
PUBLIC_DIR = BASE_DIR / "public"
PORTS_FILE = BASE_DIR / "ports.json"

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "4.206.104.502.70")
SECRET_KEY = os.getenv("SECRET_KEY", "apih9s5-secret-liquid-glass")
FLASK_PORT = int(os.getenv("FLASK_PORT", "8080"))

PROJECTS_DIR.mkdir(exist_ok=True)
PUBLIC_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=str(PUBLIC_DIR), static_url_path="")
app.secret_key = SECRET_KEY

# ─── In-Memory State ──────────────────────────────────────────────────────────
processes = {}      # slug -> {"pid": int, "port": int, "log_file": str}
process_lock = threading.Lock()
file_lock = threading.Lock()  # Simple thread lock for file operations

# ─── File Helpers (no fcntl for Termux compatibility) ──────────────────────

def atomic_read_json(path):
    """Read JSON with thread safety."""
    if not path.exists():
        return {}
    with file_lock:
        with open(path, "r") as f:
            return json.load(f)

def atomic_write_json(path, data):
    """Write JSON with thread safety."""
    with file_lock:
        # Write to temp file first, then rename for atomicity
        temp_path = path.with_suffix(".tmp")
        with open(temp_path, "w") as f:
            json.dump(data, f, indent=2)
        # Rename is atomic on POSIX systems
        temp_path.replace(path)

def load_ports_registry():
    return atomic_read_json(PORTS_FILE)

def save_ports_registry(data):
    atomic_write_json(PORTS_FILE, data)

def is_port_available(port):
    """Check if a port is available without psutil (works on Android/Termux)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex(('127.0.0.1', port))
            return result != 0
    except Exception:
        return True

def get_next_available_port(start=5000):
    registry = load_ports_registry()
    used_ports = set()
    for slug, info in registry.items():
        used_ports.add(info.get("port", 0))

    # Use psutil to detect system-wide used ports (fallback to socket check)
    try:
        for conn in psutil.net_connections():
            if conn.laddr:
                used_ports.add(conn.laddr.port)
    except (PermissionError, psutil.AccessDenied):
        # Android/Termux: psutil can't read /proc/net/tcp, fallback to socket
        pass

    port = start
    while port in used_ports or not is_port_available(port):
        port += 1
        if port > 65535:
            raise RuntimeError("No available ports found")
    return port

def get_project_meta(slug):
    meta_path = PROJECTS_DIR / slug / ".meta.json"
    if meta_path.exists():
        with open(meta_path, "r") as f:
            return json.load(f)
    return None

def save_project_meta(slug, meta):
    meta_path = PROJECTS_DIR / slug / ".meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

def get_all_projects():
    projects = []
    if PROJECTS_DIR.exists():
        for slug_dir in sorted(PROJECTS_DIR.iterdir()):
            if slug_dir.is_dir():
                meta = get_project_meta(slug_dir.name)
                if meta:
                    registry = load_ports_registry()
                    reg = registry.get(slug_dir.name, {})
                    meta["slug"] = slug_dir.name
                    meta["port"] = reg.get("port")
                    meta["pid"] = reg.get("pid")
                    meta["status"] = "active" if is_process_running(reg.get("pid")) else "stopped"
                    projects.append(meta)
    return projects

def is_process_running(pid):
    if pid is None:
        return False
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

def kill_process_tree(pid):
    try:
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        parent.terminate()
        gone, alive = psutil.wait_procs([parent], timeout=3)
        for p in alive:
            p.kill()
    except psutil.NoSuchProcess:
        pass

def install_requirements(project_dir):
    req_file = project_dir / "requirements.txt"
    if req_file.exists():
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=120
            )
        except Exception as e:
            print(f"[WARN] Failed to install requirements: {e}")

def extract_zip(zip_path, extract_to):
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_to)

def find_entry_file(project_dir, entry_name):
    """
    Recursively find the entry file.
    If the zip has a single top-level folder, move its contents up.
    """
    # Check direct
    direct = project_dir / entry_name
    if direct.exists():
        return direct

    # Check immediate subdirectories (if only one and contains entry)
    subdirs = [d for d in project_dir.iterdir() if d.is_dir()]
    if len(subdirs) == 1:
        nested = subdirs[0] / entry_name
        if nested.exists():
            # Move everything from subdir to project root
            for item in subdirs[0].iterdir():
                dest = project_dir / item.name
                if dest.exists():
                    shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
                shutil.move(str(item), str(dest))
            subdirs[0].rmdir()
            return project_dir / entry_name

    # Recursively search for the entry file (depth 2)
    for root, dirs, files in os.walk(project_dir):
        if entry_name in files:
            return Path(root) / entry_name
        # Limit depth to avoid huge scans
        depth = root.replace(str(project_dir), '').count(os.sep)
        if depth > 2:
            break
    return None

# ─── Authentication ───────────────────────────────────────────────────────────

def require_auth():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.form.get("token") or (request.json.get("token") if request.is_json else None)
    if token != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# STATIC ASSETS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/bg-admin.jpg")
def serve_bg_admin():
    return send_from_directory(str(PUBLIC_DIR), "bg-admin.jpg")

@app.route("/bg-user.jpg")
def serve_bg_user():
    return send_from_directory(str(PUBLIC_DIR), "bg-user.jpg")

@app.route("/logo.png")
def serve_logo():
    return send_from_directory(str(PUBLIC_DIR), "logo.png")

# ═══════════════════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/auth", methods=["POST"])
def api_auth():
    data = request.get_json(silent=True) or {}
    if data.get("password") == ADMIN_PASSWORD:
        return jsonify({"success": True, "token": ADMIN_PASSWORD})
    return jsonify({"success": False, "error": "Invalid password"}), 401

@app.route("/api/upload", methods=["POST"])
def api_upload():
    auth = require_auth()
    if auth:
        return auth

    if "zipfile" not in request.files:
        return jsonify({"error": "No ZIP file provided"}), 400

    file = request.files["zipfile"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    slug = request.form.get("slug", "").strip().lower()
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    port_str = request.form.get("port", "auto").strip()
    entry_file = request.form.get("entry_file", "main.py").strip()
    screenshot = request.form.get("screenshot", "").strip()
    external_url = request.form.get("external_url", "").strip()
    source_download = request.form.get("source_download", "").strip()

    if not slug or not title:
        return jsonify({"error": "Slug and title are required"}), 400

    if not slug.replace("-", "").replace("_", "").isalnum():
        return jsonify({"error": "Slug must be alphanumeric with hyphens/underscores only"}), 400

    project_dir = PROJECTS_DIR / slug
    if project_dir.exists():
        return jsonify({"error": f"Project '{slug}' already exists"}), 409

    if port_str.lower() == "auto":
        port = get_next_available_port()
    else:
        try:
            port = int(port_str)
            if port < 5000:
                return jsonify({"error": "Port must be 5000+"}), 400
        except ValueError:
            return jsonify({"error": "Invalid port"}), 400

    project_dir.mkdir(parents=True)
    zip_path = project_dir / ".source.zip"
    file.save(str(zip_path))

    try:
        extract_zip(zip_path, project_dir)
    except Exception as e:
        shutil.rmtree(project_dir)
        return jsonify({"error": f"Failed to extract ZIP: {e}"}), 500

    entry_path = find_entry_file(project_dir, entry_file)
    if not entry_path:
        # Try common alternatives
        for alt in ["app.py", "run.py", "index.py", "server.py"]:
            entry_path = find_entry_file(project_dir, alt)
            if entry_path:
                entry_file = alt
                break

    if not entry_path:
        shutil.rmtree(project_dir)
        return jsonify({"error": f"Entry file '{entry_file}' not found in project"}), 400

    install_requirements(project_dir)

    meta = {
        "title": title,
        "description": description,
        "port": port,
        "entry_file": entry_file,
        "screenshot": screenshot,
        "external_url": external_url,
        "source_download": source_download,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    save_project_meta(slug, meta)

    registry = load_ports_registry()
    registry[slug] = {"port": port, "pid": None, "entry_file": entry_file}
    save_ports_registry(registry)

    return jsonify({"success": True, "slug": slug, "port": port})

@app.route("/api/start", methods=["POST"])
def api_start():
    auth = require_auth()
    if auth:
        return auth

    data = request.get_json(silent=True) or {}
    slug = data.get("slug", "").strip().lower()

    if not slug:
        return jsonify({"error": "Slug required"}), 400

    meta = get_project_meta(slug)
    if not meta:
        return jsonify({"error": "Project not found"}), 404

    registry = load_ports_registry()
    reg = registry.get(slug, {})

    if is_process_running(reg.get("pid")):
        return jsonify({"success": True, "message": "Already running", "port": reg["port"], "pid": reg["pid"]})

    project_dir = PROJECTS_DIR / slug
    entry_file = meta.get("entry_file", "main.py")
    port = meta.get("port", reg.get("port", get_next_available_port()))

    if not is_port_available(port):
        port = get_next_available_port()

    log_file = project_dir / ".process.log"
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["FLASK_PORT"] = str(port)

    try:
        with open(log_file, "a") as log:
            log.write(f"\n--- [{datetime.now().isoformat()}] Starting {slug} on port {port} ---\n")
            log.flush()
            # Use shell=False for security, pass args as list
            proc = subprocess.Popen(
                [sys.executable, entry_file, f"--port={port}"],
                cwd=str(project_dir),
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None
            )
    except Exception as e:
        return jsonify({"error": f"Failed to start: {e}"}), 500

    time.sleep(1.5)
    if proc.poll() is not None:
        with open(log_file, "r") as f:
            logs = f.read()[-2000:]
        return jsonify({"error": "Process exited immediately", "logs": logs}), 500

    registry[slug] = {"port": port, "pid": proc.pid, "entry_file": entry_file}
    save_ports_registry(registry)

    if meta.get("port") != port:
        meta["port"] = port
        meta["updated_at"] = datetime.now().isoformat()
        save_project_meta(slug, meta)

    with process_lock:
        processes[slug] = {"pid": proc.pid, "port": port, "log_file": str(log_file)}

    return jsonify({"success": True, "port": port, "pid": proc.pid})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    auth = require_auth()
    if auth:
        return auth

    data = request.get_json(silent=True) or {}
    slug = data.get("slug", "").strip().lower()

    if not slug:
        return jsonify({"error": "Slug required"}), 400

    registry = load_ports_registry()
    reg = registry.get(slug, {})
    pid = reg.get("pid")

    if pid and is_process_running(pid):
        kill_process_tree(pid)

    registry[slug]["pid"] = None
    save_ports_registry(registry)

    with process_lock:
        if slug in processes:
            del processes[slug]

    return jsonify({"success": True, "message": "Stopped"})

@app.route("/api/restart", methods=["POST"])
def api_restart():
    auth = require_auth()
    if auth:
        return auth

    data = request.get_json(silent=True) or {}
    slug = data.get("slug", "").strip().lower()

    registry = load_ports_registry()
    reg = registry.get(slug, {})
    pid = reg.get("pid")
    if pid and is_process_running(pid):
        kill_process_tree(pid)
    registry[slug]["pid"] = None
    save_ports_registry(registry)

    with process_lock:
        if slug in processes:
            del processes[slug]

    time.sleep(0.5)

    meta = get_project_meta(slug)
    if not meta:
        return jsonify({"error": "Project not found"}), 404

    project_dir = PROJECTS_DIR / slug
    entry_file = meta.get("entry_file", "main.py")
    port = meta.get("port", reg.get("port", get_next_available_port()))

    if not is_port_available(port):
        port = get_next_available_port()

    log_file = project_dir / ".process.log"
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["FLASK_PORT"] = str(port)

    try:
        with open(log_file, "a") as log:
            log.write(f"\n--- [{datetime.now().isoformat()}] Restarting {slug} on port {port} ---\n")
            log.flush()
            proc = subprocess.Popen(
                [sys.executable, entry_file, f"--port={port}"],
                cwd=str(project_dir),
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None
            )
    except Exception as e:
        return jsonify({"error": f"Failed to restart: {e}"}), 500

    time.sleep(1.5)
    if proc.poll() is not None:
        with open(log_file, "r") as f:
            logs = f.read()[-2000:]
        return jsonify({"error": "Process exited immediately", "logs": logs}), 500

    registry[slug] = {"port": port, "pid": proc.pid, "entry_file": entry_file}
    save_ports_registry(registry)

    if meta.get("port") != port:
        meta["port"] = port
        meta["updated_at"] = datetime.now().isoformat()
        save_project_meta(slug, meta)

    with process_lock:
        processes[slug] = {"pid": proc.pid, "port": port, "log_file": str(log_file)}

    return jsonify({"success": True, "port": port, "pid": proc.pid})

@app.route("/api/projects/<slug>/edit", methods=["POST"])
def api_edit_project(slug):
    auth = require_auth()
    if auth:
        return auth

    meta = get_project_meta(slug)
    if not meta:
        return jsonify({"error": "Project not found"}), 404

    data = request.get_json(silent=True) or {}

    meta["title"] = data.get("title", meta["title"])
    meta["description"] = data.get("description", meta["description"])
    meta["screenshot"] = data.get("screenshot", meta.get("screenshot", ""))
    meta["external_url"] = data.get("external_url", meta.get("external_url", ""))
    meta["source_download"] = data.get("source_download", meta.get("source_download", ""))
    meta["updated_at"] = datetime.now().isoformat()

    save_project_meta(slug, meta)
    return jsonify({"success": True})

@app.route("/api/projects/<slug>", methods=["DELETE"])
def api_delete_project(slug):
    auth = require_auth()
    if auth:
        return auth

    registry = load_ports_registry()
    reg = registry.get(slug, {})
    pid = reg.get("pid")
    if pid and is_process_running(pid):
        kill_process_tree(pid)

    if slug in registry:
        del registry[slug]
        save_ports_registry(registry)

    project_dir = PROJECTS_DIR / slug
    if project_dir.exists():
        shutil.rmtree(project_dir)

    with process_lock:
        if slug in processes:
            del processes[slug]

    return jsonify({"success": True})

@app.route("/api/projects", methods=["GET"])
def api_list_projects():
    auth = require_auth()
    if auth:
        return auth
    return jsonify(get_all_projects())

@app.route("/api/projects/public", methods=["GET"])
def api_list_public():
    projects = get_all_projects()
    public = []
    for p in projects:
        if p.get("status") == "active":
            public.append({
                "slug": p["slug"],
                "title": p["title"],
                "description": p["description"],
                "screenshot": p.get("screenshot", ""),
                "external_url": p.get("external_url", ""),
                "source_download": p.get("source_download", ""),
                "status": p["status"],
                "created_at": p.get("created_at", "")
            })
    return jsonify(public)

@app.route("/api/download/<slug>", methods=["GET"])
def api_download(slug):
    auth = require_auth()
    if auth:
        return auth

    zip_path = PROJECTS_DIR / slug / ".source.zip"
    if not zip_path.exists():
        return jsonify({"error": "Source not found"}), 404

    return send_file(str(zip_path), as_attachment=True, download_name=f"{slug}.source.zip")

@app.route("/api/logs/<slug>", methods=["GET"])
def api_logs(slug):
    auth = require_auth()
    if auth:
        return auth

    log_file = PROJECTS_DIR / slug / ".process.log"
    if not log_file.exists():
        return jsonify({"logs": ""})

    with open(log_file, "r") as f:
        content = f.read()
    return jsonify({"logs": content[-5000:]})

# ═══════════════════════════════════════════════════════════════════════════════
# PROXY ROUTE (must be LAST to avoid catching static files)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/<slug>/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.route("/<slug>/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def proxy(slug, path):
    """Proxy requests to the project's Flask server."""
    # Skip API routes and static files
    if slug in ("api", "bg-admin.jpg", "bg-user.jpg", "logo.png"):
        abort(404)

    registry = load_ports_registry()
    reg = registry.get(slug)

    if not reg:
        static_index = PROJECTS_DIR / slug / "index.html"
        if static_index.exists():
            return send_file(str(static_index))
        abort(404)

    port = reg.get("port")
    pid = reg.get("pid")

    if not port or not is_process_running(pid):
        abort(503, description="Service unavailable - project not running")

    target_url = f"http://127.0.0.1:{port}/{path}"
    if request.query_string:
        target_url += "?" + request.query_string.decode()

    method = request.method
    headers = {k: v for k, v in request.headers if k.lower() not in ("host", "content-length")}
    headers["Host"] = f"127.0.0.1:{port}"

    try:
        resp = requests.request(
            method=method,
            url=target_url,
            headers=headers,
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            stream=True,
            timeout=30
        )
    except requests.exceptions.ConnectionError:
        abort(503, description="Service unavailable - connection refused")
    except requests.exceptions.Timeout:
        abort(504, description="Gateway timeout")

    excluded_headers = ["content-encoding", "content-length", "transfer-encoding", "connection"]
    response_headers = [(name, value) for name, value in resp.headers.items()
                        if name.lower() not in excluded_headers]

    def generate():
        for chunk in resp.iter_content(chunk_size=4096):
            yield chunk

    return Response(generate(), status=resp.status_code, headers=response_headers)

# ═══════════════════════════════════════════════════════════════════════════════
# FRONTEND
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_file(str(PUBLIC_DIR / "index.html"))

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    registry = load_ports_registry()
    for slug, info in list(registry.items()):
        if is_process_running(info.get("pid")):
            with process_lock:
                processes[slug] = {
                    "pid": info["pid"],
                    "port": info["port"],
                    "log_file": str(PROJECTS_DIR / slug / ".process.log")
                }
        else:
            registry[slug]["pid"] = None
    save_ports_registry(registry)

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                    APIH9S5 HOSTING PLATFORM                  ║
║              Liquid Glass Theme | Kali Dragon Admin          ║
╠══════════════════════════════════════════════════════════════╣
║  Admin Panel:  http://localhost:{FLASK_PORT}/#/9x              ║
║  Public Gallery: http://localhost:{FLASK_PORT}/                ║
╚══════════════════════════════════════════════════════════════╝
    """)
    app.run(host="0.0.0.0", port=FLASK_PORT, threaded=True, debug=False)