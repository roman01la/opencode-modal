"""
Modal app for OpenCode — Multi-sandbox portal.

Provides a web UI with password login, a dashboard to manage multiple
sandboxes (projects), and per-sandbox OpenCode instances. Each sandbox
gets an isolated subfolder on a shared persistent volume.

Prerequisites:
    1. pip install modal
    2. modal setup
    3. Create a Modal secret named "opencode-secret" with key
       OPENCODE_SERVER_PASSWORD set to your desired password.
       (via https://modal.com/secrets or `modal secret create`)

Usage:
    # Deploy (persistent stable URL)
    modal deploy modal_app.py

    # Development (ephemeral URL)
    modal serve modal_app.py
"""

import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from string import Template

import modal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MINUTES = 60
HOURS = 60 * MINUTES
OPENCODE_PORT = 4096
SANDBOX_TIMEOUT = 24 * HOURS
PASSWORD_SECRET_NAME = "opencode-secret"
VOLUME_MOUNT = "/data"
WORKSPACE_ROOT = f"{VOLUME_MOUNT}/workspaces"
REGISTRY_PATH = f"{VOLUME_MOUNT}/registry.json"
APP_NAME = "openportal"
COOKIE_NAME = "openportal_session"

app = modal.App(APP_NAME)

# Shared persistent volume for all sandbox workspaces and the registry file.
# Using v2 for in-sandbox `sync` support (persists data without SDK calls).
volume = modal.Volume.from_name("openportal-workspace-v2", create_if_missing=True, version=2)


# ---------------------------------------------------------------------------
# Sandbox image
# ---------------------------------------------------------------------------
def _build_sandbox_image() -> modal.Image:
    """Build the container image with OpenCode installed."""
    image = (
        modal.Image.debian_slim(python_version="3.12")
        .apt_install("curl", "unzip", "git", "ca-certificates", "gnupg")
        # Install Node.js (LTS) via NodeSource
        .run_commands(
            "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -",
            "apt-get install -y nodejs",
        )
        # Install Bun
        .run_commands(
            "curl -fsSL https://bun.sh/install | bash",
        )
        # Install JDK (Eclipse Temurin 21)
        .run_commands(
            "apt-get install -y wget apt-transport-https",
            "mkdir -p /etc/apt/keyrings",
            "wget -O - https://packages.adoptium.net/artifactory/api/gpg/key/public | tee /etc/apt/keyrings/adoptium.asc",
            'echo "deb [signed-by=/etc/apt/keyrings/adoptium.asc] https://packages.adoptium.net/artifactory/deb bookworm main" | tee /etc/apt/sources.list.d/adoptium.list',
            "apt-get update",
            "apt-get install -y temurin-21-jdk",
        )
        # Install OpenCode
        .run_commands("curl -fsSL https://opencode.ai/install | bash")
        .env(
            {
                "PATH": "/root/.bun/bin:/root/.opencode/bin:/usr/local/bin:/usr/bin:/bin",
                "HOME": "/root",
                "JAVA_HOME": "/usr/lib/jvm/temurin-21-jdk-amd64",
            }
        )
    )

    # Include local OpenCode config if it exists.
    config_path = Path("~/.config/opencode/opencode.json").expanduser()
    if config_path.exists():
        print("Including config from", config_path)
        image = image.add_local_file(
            config_path, "/root/.config/opencode/opencode.json", copy=True
        )

    return image


sandbox_image = _build_sandbox_image()

# Proxy image — lightweight, only needs fastapi.
proxy_image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "fastapi", "httpx"
).add_local_dir(
    str(Path(__file__).parent / "templates"),
    "/root/templates",
    copy=True,
)


# ---------------------------------------------------------------------------
# Sandbox registry — JSON file on the volume
# ---------------------------------------------------------------------------
def _read_registry() -> list[dict]:
    """Read the sandbox registry from the volume."""
    volume.reload()
    try:
        with open(REGISTRY_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_registry(entries: list[dict]) -> None:
    """Write the sandbox registry to the volume."""
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
    with open(REGISTRY_PATH, "w") as f:
        json.dump(entries, f, indent=2)
    volume.commit()


def _add_registry_entry(entry: dict) -> None:
    """Add a new entry to the registry."""
    entries = _read_registry()
    entries.append(entry)
    _write_registry(entries)


def _get_registry_entry(sandbox_id: str) -> dict | None:
    """Look up a single registry entry by sandbox_id."""
    for entry in _read_registry():
        if entry["id"] == sandbox_id:
            return entry
    return None


def _remove_registry_entry(sandbox_id: str) -> None:
    """Remove a registry entry by sandbox_id."""
    entries = _read_registry()
    entries = [e for e in entries if e["id"] != sandbox_id]
    _write_registry(entries)


# ---------------------------------------------------------------------------
# Sandbox lifecycle helpers
# ---------------------------------------------------------------------------
def _make_setup_script(workspace_dir: str) -> str:
    """Build the bash setup script for a sandbox."""
    return f"""set -e
mkdir -p {workspace_dir}/.opencode-data
mkdir -p /root/.local/share
ln -sfn {workspace_dir}/.opencode-data /root/.local/share/opencode
# Share auth across all sandboxes via a single file on the volume.
mkdir -p {VOLUME_MOUNT}/shared
if [ ! -f {VOLUME_MOUNT}/shared/auth.json ]; then
    echo '{{}}' > {VOLUME_MOUNT}/shared/auth.json
fi
ln -sf {VOLUME_MOUNT}/shared/auth.json {workspace_dir}/.opencode-data/auth.json
if [ ! -d {workspace_dir}/.git ]; then
    git init {workspace_dir}
fi
exec opencode serve --hostname=0.0.0.0 --port={OPENCODE_PORT}
"""


def _create_sandbox(
    name: str,
    cpu: float = 4,
    memory: int = 4096,
    gpu_type: str = "",
    gpu_count: int = 0,
) -> dict:
    """Create a new sandbox and register it."""
    sandbox_id = uuid.uuid4().hex[:12]
    workspace_dir = f"{WORKSPACE_ROOT}/{sandbox_id}"
    deployed_app = modal.App.lookup(APP_NAME)

    gpu = None
    if gpu_type and gpu_count > 0:
        gpu = f"{gpu_type}:{gpu_count}" if gpu_count > 1 else gpu_type

    sb = modal.Sandbox.create(
        "bash",
        "-c",
        _make_setup_script(workspace_dir),
        name=f"opencode-{sandbox_id}",
        encrypted_ports=[OPENCODE_PORT],
        timeout=SANDBOX_TIMEOUT,
        cpu=cpu,
        memory=memory,
        gpu=gpu,
        image=sandbox_image,
        app=deployed_app,
        volumes={VOLUME_MOUNT: volume},
        workdir=workspace_dir,
    )

    entry = {
        "id": sandbox_id,
        "name": name,
        "modal_sandbox_id": sb.object_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cpu": cpu,
        "memory": memory,
        "gpu_type": gpu_type,
        "gpu_count": gpu_count,
    }
    _add_registry_entry(entry)
    return entry


def _get_sandbox_status(entry: dict) -> str:
    """Return 'running' or 'stopped' for a registry entry."""
    try:
        sb = modal.Sandbox.from_id(entry["modal_sandbox_id"])
        result = sb.poll()
        if result is None:
            return "running"
        return "stopped"
    except Exception:
        return "stopped"


def _get_running_sandbox(entry: dict) -> modal.Sandbox | None:
    """Get a running Sandbox object, or None if stopped."""
    try:
        sb = modal.Sandbox.from_id(entry["modal_sandbox_id"])
        if sb.poll() is None:
            return sb
    except Exception:
        pass
    return None


def _snapshot_and_terminate(sb: modal.Sandbox, sandbox_id: str) -> None:
    """Take a filesystem snapshot, save its ID, and terminate a running sandbox."""
    try:
        snapshot_image = sb.snapshot_filesystem()
        # Persist snapshot image ID in the registry.
        entries = _read_registry()
        for e in entries:
            if e["id"] == sandbox_id:
                e["snapshot_image_id"] = snapshot_image.object_id
                break
        _write_registry(entries)
    except Exception:
        pass
    sb.terminate()


def _resolve_sandbox_image(entry: dict) -> modal.Image:
    """Return the snapshot image if one exists, otherwise the base sandbox image."""
    snapshot_id = entry.get("snapshot_image_id")
    if snapshot_id:
        try:
            return modal.Image.from_id(snapshot_id)
        except Exception:
            pass
    return sandbox_image


def _start_sandbox(entry: dict) -> dict:
    """Start a new sandbox container for an existing registry entry."""
    # Ensure the old sandbox is fully terminated before reusing the name.
    try:
        old_sb = modal.Sandbox.from_id(entry["modal_sandbox_id"])
        if old_sb.poll() is None:
            _snapshot_and_terminate(old_sb, entry["id"])
            # Re-read entry to pick up the snapshot_image_id just saved.
            entry = _get_registry_entry(entry["id"]) or entry
    except Exception:
        pass

    sandbox_id = entry["id"]
    workspace_dir = f"{WORKSPACE_ROOT}/{sandbox_id}"
    deployed_app = modal.App.lookup(APP_NAME)

    cpu = entry.get("cpu", 4.0)
    memory = entry.get("memory", 8192)
    gpu_type = entry.get("gpu_type", "")
    gpu_count = entry.get("gpu_count", 0)
    gpu = None
    if gpu_type and gpu_count > 0:
        gpu = f"{gpu_type}:{gpu_count}" if gpu_count > 1 else gpu_type

    image = _resolve_sandbox_image(entry)

    sb = modal.Sandbox.create(
        "bash",
        "-c",
        _make_setup_script(workspace_dir),
        name=f"opencode-{sandbox_id}",
        encrypted_ports=[OPENCODE_PORT],
        timeout=SANDBOX_TIMEOUT,
        cpu=cpu,
        memory=memory,
        gpu=gpu,
        image=image,
        app=deployed_app,
        volumes={VOLUME_MOUNT: volume},
        workdir=workspace_dir,
    )

    # Update the modal_sandbox_id in the registry.
    entries = _read_registry()
    for e in entries:
        if e["id"] == sandbox_id:
            e["modal_sandbox_id"] = sb.object_id
            break
    _write_registry(entries)

    entry["modal_sandbox_id"] = sb.object_id
    return entry


def _delete_sandbox(entry: dict) -> None:
    """Delete a sandbox: terminate it, remove its workspace, and unregister."""
    # Terminate if still running (no snapshot needed since we're deleting).
    try:
        sb = modal.Sandbox.from_id(entry["modal_sandbox_id"])
        if sb.poll() is None:
            sb.terminate()
    except Exception:
        pass

    # Remove workspace folder from the volume using the volume API
    # (shutil.rmtree on the local mount doesn't reliably propagate deletions).
    workspace_dir = f"workspaces/{entry['id']}"
    try:
        volume.remove_file(workspace_dir, recursive=True)
    except Exception:
        pass

    # Remove from registry.
    _remove_registry_entry(entry["id"])


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _get_password() -> str:
    """Get the portal password from the environment."""
    return os.environ["OPENCODE_SERVER_PASSWORD"]


def _make_token(password: str) -> str:
    """Create a signed session token from the password."""
    return hmac.new(
        password.encode(), b"openportal-session", hashlib.sha256
    ).hexdigest()


def _check_token(token: str) -> bool:
    """Verify a session token."""
    expected = _make_token(_get_password())
    return hmac.compare_digest(token, expected)


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------
# Use the container path if available (Modal deploys templates there),
# otherwise fall back to the local path next to modal_app.py.
_CONTAINER_TEMPLATES = Path("/root/templates")
_LOCAL_TEMPLATES = Path(__file__).parent / "templates"
TEMPLATES_DIR = _CONTAINER_TEMPLATES if _CONTAINER_TEMPLATES.is_dir() else _LOCAL_TEMPLATES


def _load_template(name: str) -> Template:
    """Load an HTML template from the templates/ directory."""
    return Template((TEMPLATES_DIR / name).read_text())


# Pre-load shared partial.
_PWA_HEAD = (TEMPLATES_DIR / "head.html").read_text()


def _render_login_page(error: str = "") -> str:
    """Render the login page template."""
    return _load_template("login.html").safe_substitute(
        pwa_head=_PWA_HEAD, error=error
    )


def _render_sandbox_card(s: dict) -> str:
    """Render a single sandbox card HTML fragment."""
    status = s["status"]
    status_dot = (
        '<span style="color:#22c55e">&#9679;</span>'
        if status == "running"
        else '<span style="color:#737373">&#9679;</span>'
    )

    # Resource summary
    resources = _format_resources(s)

    if status == "running":
        actions = (
            f'<a href="/sandboxes/{s["id"]}/open" class="btn btn-primary">Open</a>'
            f' <form method="POST" action="/sandboxes/{s["id"]}/stop" style="display:inline">'
            f'<button type="submit" class="btn btn-secondary">Stop</button></form>'
            f' <form method="POST" action="/sandboxes/{s["id"]}/delete" style="display:inline"'
            f' onsubmit="return confirm(\'Delete {s["name"]}? This will permanently remove its workspace.\')">'
            f'<button type="submit" class="btn btn-danger">Delete</button></form>'
        )
    else:
        actions = (
            f'<form method="POST" action="/sandboxes/{s["id"]}/start" style="display:inline">'
            f'<button type="submit" class="btn btn-primary">Start</button></form>'
            f' <form method="POST" action="/sandboxes/{s["id"]}/delete" style="display:inline"'
            f' onsubmit="return confirm(\'Delete {s["name"]}? This will permanently remove its workspace.\')">'
            f'<button type="submit" class="btn btn-danger">Delete</button></form>'
        )

    return f"""<div class="sandbox-card">
      <div class="sandbox-info">
        <div class="sandbox-name">{s["name"]}</div>
        <div class="sandbox-meta">{status_dot} {status} &middot; {resources}</div>
      </div>
      <div class="sandbox-actions">{actions}</div>
    </div>"""


def _dashboard_page(sandboxes: list[dict]) -> str:
    """Render the dashboard HTML with a list of sandboxes."""
    if sandboxes:
        cards = "".join(_render_sandbox_card(s) for s in sandboxes)
        sandbox_list = f'<div class="sandbox-list">{cards}</div>'
    else:
        sandbox_list = '<p style="color:#737373;text-align:center;padding:2rem 0">No sandboxes yet. Create one below.</p>'

    return _load_template("dashboard.html").safe_substitute(
        pwa_head=_PWA_HEAD, sandbox_list=sandbox_list
    )


def _format_resources(entry: dict) -> str:
    """Format resource info as a short summary string."""
    cpu = entry.get("cpu", 4)
    mem = entry.get("memory", 8192)
    gpu_type = entry.get("gpu_type", "")
    gpu_count = entry.get("gpu_count", 0)
    mem_display = f"{mem}MB" if mem < 1024 else f"{mem // 1024}GB"
    resources = f"{int(cpu)} CPU, {mem_display}"
    if gpu_type and gpu_count > 0:
        gpu_label = f"{gpu_count}x {gpu_type}" if gpu_count > 1 else gpu_type
        resources += f", {gpu_label}"
    return resources


def _render_iframe_page(
    name: str,
    tunnel_url: str,
    resources: str,
    sandbox_id: str = "",
    dashboard_url: str = "",
) -> str:
    """Render the iframe page for an open sandbox."""
    return _load_template("iframe.html").safe_substitute(
        pwa_head=_PWA_HEAD,
        name=name,
        tunnel_url=tunnel_url,
        resources=resources,
        sandbox_id=sandbox_id,
        dashboard_url=dashboard_url,
    )


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
def _create_fastapi_app():
    """Build the FastAPI app with all routes."""
    from fastapi import FastAPI, Form, Request
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

    web = FastAPI()

    # -- Manifest & icon (PWA) ------------------------------------------------
    _FAVICON_SVG = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<circle cx="50" cy="50" r="50" fill="#0a0a0a"/>'
        '<text x="50" y="50" text-anchor="middle" dominant-baseline="central" '
        'font-family="system-ui,-apple-system,sans-serif" font-size="58" '
        'font-weight="700" fill="#e5e5e5">O</text></svg>'
    )

    @web.get("/icon.svg")
    def icon():
        from fastapi.responses import Response

        return Response(content=_FAVICON_SVG, media_type="image/svg+xml")

    @web.get("/manifest.json")
    def manifest():
        return JSONResponse(
            {
                "name": "OpenPortal",
                "short_name": "OpenPortal",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#0a0a0a",
                "theme_color": "#0a0a0a",
                "icons": [
                    {
                        "src": "/icon.svg",
                        "sizes": "any",
                        "type": "image/svg+xml",
                        "purpose": "any",
                    }
                ],
            },
            media_type="application/manifest+json",
        )

    # -- Auth dependency ---------------------------------------------------
    class _AuthRedirect(Exception):
        """Raised when a request is not authenticated."""

    @web.exception_handler(_AuthRedirect)
    async def _auth_redirect_handler(request: Request, exc: _AuthRedirect):
        return RedirectResponse("/", status_code=303)

    def _require_auth(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if not token or not _check_token(token):
            raise _AuthRedirect()

    # -- Routes ------------------------------------------------------------

    @web.get("/")
    def index(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if token and _check_token(token):
            return RedirectResponse("/dashboard", status_code=303)
        return HTMLResponse(_render_login_page())

    @web.post("/login")
    def login(request: Request, password: str = Form(...)):
        if not hmac.compare_digest(password, _get_password()):
            html = _render_login_page('<p class="error">Invalid password</p>')
            return HTMLResponse(html, status_code=401)
        token = _make_token(password)
        response = RedirectResponse("/dashboard", status_code=303)
        is_secure = request.url.scheme == "https"
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=is_secure,
            samesite="lax",
            max_age=7 * 24 * HOURS,
        )
        return response

    @web.post("/logout")
    def logout():
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    @web.get("/dashboard")
    def dashboard(request: Request):
        _require_auth(request)
        entries = _read_registry()
        sandboxes = []
        for entry in entries:
            status = _get_sandbox_status(entry)
            sandboxes.append({**entry, "status": status})
        html = _dashboard_page(sandboxes)
        return HTMLResponse(html)

    @web.post("/sandboxes")
    def create_sandbox(
        request: Request,
        name: str = Form(...),
        cpu: str = Form("4"),
        memory: str = Form("4096"),
        gpu_type: str = Form(""),
        gpu_count: str = Form("1"),
    ):
        _require_auth(request)
        _create_sandbox(
            name,
            cpu=float(cpu),
            memory=int(memory),
            gpu_type=gpu_type,
            gpu_count=int(gpu_count) if gpu_type else 0,
        )
        return RedirectResponse("/dashboard", status_code=303)

    @web.post("/sandboxes/{sandbox_id}/start")
    def start_sandbox(sandbox_id: str, request: Request):
        _require_auth(request)
        entry = _get_registry_entry(sandbox_id)
        if entry:
            _start_sandbox(entry)
        return RedirectResponse("/dashboard", status_code=303)

    @web.post("/sandboxes/{sandbox_id}/stop")
    def stop_sandbox(sandbox_id: str, request: Request):
        _require_auth(request)
        entry = _get_registry_entry(sandbox_id)
        if entry:
            try:
                sb = modal.Sandbox.from_id(entry["modal_sandbox_id"])
                if sb.poll() is None:
                    _snapshot_and_terminate(sb, sandbox_id)
            except Exception:
                pass
        return RedirectResponse("/dashboard", status_code=303)

    @web.post("/sandboxes/{sandbox_id}/delete")
    def delete_sandbox(sandbox_id: str, request: Request):
        _require_auth(request)
        entry = _get_registry_entry(sandbox_id)
        if entry:
            _delete_sandbox(entry)
        return RedirectResponse("/dashboard", status_code=303)

    @web.get("/sandboxes/{sandbox_id}/open")
    def open_sandbox(sandbox_id: str, request: Request):
        _require_auth(request)
        entry = _get_registry_entry(sandbox_id)
        if not entry:
            return RedirectResponse("/dashboard", status_code=303)

        sb = _get_running_sandbox(entry)
        if not sb:
            return RedirectResponse("/dashboard", status_code=303)

        tunnel = sb.tunnels()[OPENCODE_PORT]
        name = entry.get("name", sandbox_id)
        resources = _format_resources(entry)
        dashboard_url = sb.get_dashboard_url()
        html = _render_iframe_page(
            name, tunnel.url, resources,
            sandbox_id=sandbox_id,
            dashboard_url=dashboard_url,
        )
        return HTMLResponse(html)

    @web.get("/api/sandboxes/{sandbox_id}/stats")
    def sandbox_stats(sandbox_id: str, request: Request):
        """Return live CPU/RAM/GPU usage for a running sandbox."""
        token = request.cookies.get(COOKIE_NAME)
        if not token or not _check_token(token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        entry = _get_registry_entry(sandbox_id)
        if not entry:
            return JSONResponse({"error": "Not found"}, status_code=404)

        sb = _get_running_sandbox(entry)
        if not sb:
            return JSONResponse({"error": "Sandbox not running"}, status_code=404)

        stats: dict = {}

        # --- CPU: sample cgroup cpuacct over a short interval ---
        # /proc/loadavg is unreliable in containers; use cgroup v1 cpuacct
        try:
            p = sb.exec(
                "sh", "-c",
                "cat /sys/fs/cgroup/cpuacct/cpuacct.usage; sleep 0.5; cat /sys/fs/cgroup/cpuacct/cpuacct.usage",
            )
            lines = "".join(p.stdout).strip().split("\n")
            p.wait()
            t1 = int(lines[0])
            t2 = int(lines[1])
            # cpuacct.usage is in nanoseconds; delta over 0.5s interval
            delta_ns = t2 - t1
            interval_ns = 500_000_000  # 0.5s
            cpu_count = entry.get("cpu", 1)
            cpu_pct = (delta_ns / interval_ns / cpu_count) * 100
            stats["cpu_pct"] = round(min(cpu_pct, 100), 1)
        except Exception:
            stats["cpu_pct"] = None

        # --- RAM: read cgroup memory stats (cgroup v1) ---
        try:
            p = sb.exec("cat", "/sys/fs/cgroup/memory/memory.usage_in_bytes")
            mem_used_raw = "".join(p.stdout).strip()
            p.wait()
            mem_used = int(mem_used_raw)

            p2 = sb.exec("cat", "/sys/fs/cgroup/memory/memory.limit_in_bytes")
            mem_max_raw = "".join(p2.stdout).strip()
            p2.wait()
            # cgroup v1 reports a huge number when unlimited
            mem_max = int(mem_max_raw)
            if mem_max > 2**50:
                mem_max = entry.get("memory", 4096) * 1024 * 1024

            stats["ram_used_mb"] = round(mem_used / (1024 * 1024), 1)
            stats["ram_max_mb"] = round(mem_max / (1024 * 1024), 1)
            stats["ram_pct"] = round(mem_used / mem_max * 100, 1) if mem_max > 0 else 0
        except Exception as e:
            stats["ram_used_mb"] = None
            stats["ram_pct"] = None

        # --- GPU: nvidia-smi (only if GPU configured) ---
        gpu_type = entry.get("gpu_type", "")
        if gpu_type:
            try:
                p = sb.exec(
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                )
                gpu_line = "".join(p.stdout).strip()
                p.wait()
                # Format: "42, 1234, 16384"
                parts = [x.strip() for x in gpu_line.split(",")]
                stats["gpu_util_pct"] = float(parts[0])
                stats["gpu_mem_used_mb"] = float(parts[1])
                stats["gpu_mem_total_mb"] = float(parts[2])
            except Exception:
                stats["gpu_util_pct"] = None

        return JSONResponse(stats)

    @web.post("/api/transcribe")
    async def transcribe(request: Request):
        import httpx

        # Return 401 JSON instead of redirecting for API endpoints.
        token = request.cookies.get(COOKIE_NAME)
        if not token or not _check_token(token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        form = await request.form()
        try:
            audio_file = form.get("file")
            if not audio_file:
                return JSONResponse({"error": "No audio file provided"}, status_code=400)

            api_key = os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                return JSONResponse(
                    {"error": "OPENAI_API_KEY not configured"}, status_code=500
                )

            contents = await audio_file.read()
            filename = audio_file.filename or "recording.webm"

            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (filename, contents, audio_file.content_type or "audio/webm")},
                    data={"model": "whisper-1"},
                )

            if resp.status_code != 200:
                return JSONResponse(
                    {"error": f"OpenAI API error: {resp.text}"}, status_code=resp.status_code
                )

            return JSONResponse(resp.json())
        finally:
            await form.close()

    return web


# ---------------------------------------------------------------------------
# Modal function — serves the FastAPI app
# ---------------------------------------------------------------------------
@app.function(
    image=proxy_image,
    secrets=[
        modal.Secret.from_name(PASSWORD_SECRET_NAME),
        modal.Secret.from_name("openai-secret"),
    ],
    volumes={VOLUME_MOUNT: volume},
    scaledown_window=300,
    timeout=300,
    max_containers=1,
)
@modal.concurrent(max_inputs=10)
@modal.asgi_app()
def portal():
    """Serve the OpenPortal web UI."""
    return _create_fastapi_app()
