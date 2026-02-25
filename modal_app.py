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
proxy_image = modal.Image.debian_slim(python_version="3.12").pip_install("fastapi")


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
exec opencode serve --hostname=0.0.0.0 --port={OPENCODE_PORT}
"""


def _create_sandbox(
    name: str,
    cpu: float = 4.0,
    memory: int = 8192,
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


def _start_sandbox(entry: dict) -> dict:
    """Start a new sandbox container for an existing registry entry."""
    # Ensure the old sandbox is fully terminated before reusing the name.
    try:
        old_sb = modal.Sandbox.from_id(entry["modal_sandbox_id"])
        if old_sb.poll() is None:
            old_sb.terminate()
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
    # Terminate if still running.
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
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenPortal — Login</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: system-ui, -apple-system, sans-serif;
    background: #0a0a0a; color: #e5e5e5;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh;
  }
  .card {
    background: #171717; border: 1px solid #262626; border-radius: 12px;
    padding: 2.5rem; width: 100%; max-width: 380px;
  }
  h1 { font-size: 1.25rem; margin-bottom: 1.5rem; text-align: center; }
  input[type="password"] {
    width: 100%; padding: 0.7rem 1rem; border: 1px solid #333;
    border-radius: 8px; background: #0a0a0a; color: #e5e5e5;
    font-size: 0.95rem; outline: none; margin-bottom: 1rem;
  }
  input[type="password"]:focus { border-color: #555; }
  button {
    width: 100%; padding: 0.7rem; border: none; border-radius: 8px;
    background: #e5e5e5; color: #0a0a0a; font-size: 0.95rem;
    font-weight: 600; cursor: pointer;
  }
  button:hover { background: #d4d4d4; }
  .error {
    color: #ef4444; font-size: 0.85rem; margin-bottom: 1rem; text-align: center;
  }
</style>
</head>
<body>
<div class="card">
  <h1>OpenPortal</h1>
  {error}
  <form method="POST" action="/login">
    <input type="password" name="password" placeholder="Password" autofocus required>
    <button type="submit">Sign in</button>
  </form>
</div>
</body>
</html>"""


def _dashboard_page(sandboxes: list[dict]) -> str:
    """Render the dashboard HTML with a list of sandboxes."""
    rows = ""
    for s in sandboxes:
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

        actions = ""
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

        rows += f"""<tr>
          <td>{s["name"]}</td>
          <td>{status_dot} {status}</td>
          <td style="font-size:0.8rem;color:#737373">{resources}</td>
          <td style="font-size:0.8rem;color:#737373">{s["created_at"]}</td>
          <td>{actions}</td>
        </tr>"""

    empty = ""
    if not sandboxes:
        empty = '<p style="color:#737373;text-align:center;padding:2rem 0">No sandboxes yet. Create one below.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenPortal — Dashboard</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: system-ui, -apple-system, sans-serif;
    background: #0a0a0a; color: #e5e5e5;
    min-height: 100vh; padding: 2rem;
  }}
  .container {{ max-width: 720px; margin: 0 auto; }}
  .header {{
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 2rem;
  }}
  h1 {{ font-size: 1.25rem; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 0.75rem 1rem; text-align: left; }}
  th {{
    font-size: 0.75rem; text-transform: uppercase; color: #737373;
    border-bottom: 1px solid #262626;
  }}
  tr {{ border-bottom: 1px solid #1a1a1a; }}
  tr:hover {{ background: #171717; }}
  .btn {{
    display: inline-block; padding: 0.4rem 0.9rem; border-radius: 6px;
    font-size: 0.8rem; font-weight: 600; text-decoration: none;
    cursor: pointer; border: none;
  }}
  .btn-primary {{ background: #e5e5e5; color: #0a0a0a; }}
  .btn-primary:hover {{ background: #d4d4d4; }}
  .btn-secondary {{ background: #262626; color: #e5e5e5; }}
  .btn-secondary:hover {{ background: #333; }}
  .btn-danger {{ background: #7f1d1d; color: #fca5a5; }}
  .btn-danger:hover {{ background: #991b1b; }}
  .create-form {{
    margin-top: 2rem; border-top: 1px solid #262626; padding-top: 1.5rem;
  }}
  .create-form .row {{
    display: flex; gap: 0.75rem; margin-bottom: 0.75rem;
  }}
  .create-form input, .create-form select {{
    padding: 0.6rem 1rem; border: 1px solid #333;
    border-radius: 8px; background: #0a0a0a; color: #e5e5e5;
    font-size: 0.9rem; outline: none;
  }}
  .create-form input {{ flex: 1; }}
  .create-form input:focus, .create-form select:focus {{ border-color: #555; }}
  .create-form label {{
    font-size: 0.75rem; color: #737373; display: block; margin-bottom: 0.3rem;
  }}
  .create-form .field {{ display: flex; flex-direction: column; }}
  .create-form .field-grow {{ flex: 1; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>OpenPortal</h1>
    <form method="POST" action="/logout">
      <button type="submit" class="btn btn-secondary">Logout</button>
    </form>
  </div>

  {f'<table><thead><tr><th>Name</th><th>Status</th><th>Resources</th><th>Created</th><th>Actions</th></tr></thead><tbody>{rows}</tbody></table>' if sandboxes else empty}

  <form class="create-form" method="POST" action="/sandboxes">
    <div class="row">
      <div class="field field-grow">
        <label>Name</label>
        <input type="text" name="name" placeholder="New sandbox name..." required>
      </div>
    </div>
    <div class="row">
      <div class="field">
        <label>CPU</label>
        <select name="cpu">
          <option value="1">1</option>
          <option value="2">2</option>
          <option value="4" selected>4</option>
          <option value="8">8</option>
          <option value="16">16</option>
        </select>
      </div>
      <div class="field">
        <label>RAM (MB)</label>
        <select name="memory">
          <option value="1024">1024</option>
          <option value="2048">2048</option>
          <option value="4096">4096</option>
          <option value="8192" selected>8192</option>
          <option value="16384">16384</option>
          <option value="32768">32768</option>
        </select>
      </div>
      <div class="field">
        <label>GPU</label>
        <select name="gpu_type" id="gpu_type" onchange="document.getElementById('gpu_count_field').style.display=this.value?'flex':'none'">
          <option value="">None</option>
          <option value="T4">T4</option>
          <option value="L4">L4</option>
          <option value="A10G">A10G</option>
          <option value="L40S">L40S</option>
          <option value="A100">A100</option>
          <option value="H100">H100</option>
        </select>
      </div>
      <div class="field" id="gpu_count_field" style="display:none">
        <label>GPU Count</label>
        <select name="gpu_count">
          <option value="1" selected>1</option>
          <option value="2">2</option>
          <option value="4">4</option>
        </select>
      </div>
    </div>
    <div class="row">
      <button type="submit" class="btn btn-primary">Create Sandbox</button>
    </div>
  </form>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
def _create_fastapi_app():
    """Build the FastAPI app with all routes."""
    from fastapi import FastAPI, Form, Request
    from fastapi.responses import HTMLResponse, RedirectResponse

    web = FastAPI()

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
        return HTMLResponse(LOGIN_PAGE.replace("{error}", ""))

    @web.post("/login")
    def login(request: Request, password: str = Form(...)):
        if not hmac.compare_digest(password, _get_password()):
            html = LOGIN_PAGE.replace(
                "{error}", '<p class="error">Invalid password</p>'
            )
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
        memory: str = Form("8192"),
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
                sb.terminate()
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
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{name} — OpenPortal</title>
<style>
  * {{ margin: 0; padding: 0; }}
  html, body {{ height: 100%; overflow: hidden; }}
  iframe {{ width: 100%; height: 100%; border: none; }}
</style>
</head>
<body>
<iframe src="{tunnel.url}" allow="clipboard-read; clipboard-write"></iframe>
</body>
</html>"""
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
