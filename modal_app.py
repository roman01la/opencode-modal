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
volume = modal.Volume.from_name("openportal-workspace", create_if_missing=True)


# ---------------------------------------------------------------------------
# Sandbox image
# ---------------------------------------------------------------------------
def _build_sandbox_image() -> modal.Image:
    """Build the container image with OpenCode installed."""
    image = (
        modal.Image.debian_slim(python_version="3.12")
        .apt_install("curl", "unzip", "git", "ca-certificates")
        .run_commands("curl -fsSL https://opencode.ai/install | bash")
        .env(
            {
                "PATH": "/root/.opencode/bin:/usr/local/bin:/usr/bin:/bin",
                "HOME": "/root",
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
    "fastapi"
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
if [ ! -d {workspace_dir}/.git ]; then
    git init {workspace_dir}
fi
# Periodically sync volume to persist data while running.
while true; do sleep 60; sync {VOLUME_MOUNT}; done &
exec opencode serve --hostname=0.0.0.0 --port={OPENCODE_PORT}
"""


def _create_sandbox(
    name: str,
    cpu: float = 0.5,
    memory: int = 512,
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


def _sync_and_terminate(sb: modal.Sandbox) -> None:
    """Flush volume writes and terminate a running sandbox."""
    try:
        sb.exec("sync", VOLUME_MOUNT)
    except Exception:
        pass
    sb.terminate()


def _start_sandbox(entry: dict) -> dict:
    """Start a new sandbox container for an existing registry entry."""
    # Ensure the old sandbox is fully terminated before reusing the name.
    try:
        old_sb = modal.Sandbox.from_id(entry["modal_sandbox_id"])
        if old_sb.poll() is None:
            _sync_and_terminate(old_sb)
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
    # Sync and terminate if still running.
    try:
        sb = modal.Sandbox.from_id(entry["modal_sandbox_id"])
        if sb.poll() is None:
            _sync_and_terminate(sb)
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
    cpu = s.get("cpu", 4)
    mem = s.get("memory", 8192)
    gpu_type = s.get("gpu_type", "")
    gpu_count = s.get("gpu_count", 0)
    mem_display = f"{mem}MB" if mem < 1024 else f"{mem // 1024}GB"
    resources = f"{int(cpu)} CPU, {mem_display}"
    if gpu_type and gpu_count > 0:
        gpu_label = f"{gpu_count}x {gpu_type}" if gpu_count > 1 else gpu_type
        resources += f", {gpu_label}"

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


def _render_iframe_page(name: str, tunnel_url: str) -> str:
    """Render the iframe page for an open sandbox."""
    return _load_template("iframe.html").safe_substitute(
        pwa_head=_PWA_HEAD, name=name, tunnel_url=tunnel_url
    )


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
def _create_fastapi_app():
    """Build the FastAPI app with all routes."""
    from fastapi import FastAPI, Form, Request
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

    web = FastAPI()

    # -- Manifest (PWA) ----------------------------------------------------
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
                "icons": [],
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
        cpu: str = Form("0.5"),
        memory: str = Form("512"),
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
                    _sync_and_terminate(sb)
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
        html = _render_iframe_page(name, tunnel.url)
        return HTMLResponse(html)

    return web


# ---------------------------------------------------------------------------
# Modal function — serves the FastAPI app
# ---------------------------------------------------------------------------
@app.function(
    image=proxy_image,
    secrets=[modal.Secret.from_name(PASSWORD_SECRET_NAME)],
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
