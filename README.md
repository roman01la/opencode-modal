# OpenPortal

A self-hosted portal for managing multiple [OpenCode](https://opencode.ai) instances on [Modal](https://modal.com). Each instance runs in its own sandbox with an isolated workspace on a shared persistent volume.

## Features

- **Password-protected portal** -- single login gate via HMAC-signed session cookie
- **Multi-sandbox dashboard** -- create, start, stop, and delete sandboxes from a web UI
- **Isolated workspaces** -- each sandbox gets its own subfolder on a shared Modal Volume
- **Fullscreen iframe** -- sandboxes open inside the portal (no second login, no exposed tunnel URLs)
- **Persistent state** -- workspace files and OpenCode session data survive sandbox restarts

## Prerequisites

1. Python 3.12+
2. [Modal](https://modal.com) account and CLI

```
pip install modal
modal setup
```

3. A Modal secret named `opencode-secret` with key `OPENCODE_SERVER_PASSWORD`:

```
modal secret create opencode-secret OPENCODE_SERVER_PASSWORD=<your-password>
```

4. (Optional) An OpenCode config at `~/.config/opencode/opencode.json` -- if present, it will be bundled into the sandbox image.

## Usage

### Deploy (stable URL)

```
modal deploy modal_app.py
```

### Development (ephemeral URL, hot-reload)

```
modal serve modal_app.py
```

## Architecture

The entire application is a single file (`modal_app.py`) containing:

- **Portal function** -- a FastAPI app served via `@modal.asgi_app()` that handles auth, the dashboard UI, and sandbox lifecycle management
- **Sandbox containers** -- on-demand Modal Sandboxes running `opencode serve`, each with 4 CPUs and 8 GB RAM
- **Shared volume** -- `openportal-workspace` Modal Volume mounted at `/data`, storing the sandbox registry (`registry.json`) and all workspace directories (`workspaces/<id>/`)

```
Portal (FastAPI)
  |
  |-- /              Login page
  |-- /dashboard     List sandboxes, create/start/stop/delete
  |-- /sandboxes/:id/open   Fullscreen iframe to sandbox tunnel
  |
  +-- Modal Volume (/data)
       |-- registry.json        Sandbox metadata
       +-- workspaces/
            |-- <sandbox-id-1>/  Workspace + .opencode-data/
            +-- <sandbox-id-2>/  Workspace + .opencode-data/
```

### Sandbox lifecycle

| Action | What happens |
|--------|-------------|
| **Create** | Allocates a new sandbox ID, creates a Modal Sandbox container, registers it in `registry.json` |
| **Start** | Terminates any old container for the entry, creates a fresh one, updates the registry |
| **Stop** | Terminates the running container (workspace files persist on the volume) |
| **Open** | Looks up the sandbox tunnel URL and serves it in a fullscreen iframe |
| **Delete** | Terminates the container, removes the workspace directory, removes the registry entry |

### Security model

- The portal is password-protected. A correct password sets an HMAC-signed session cookie (7-day TTL).
- Sandboxes run without their own auth. Their tunnel URLs are randomly generated Modal IDs that are not exposed to the user (loaded via iframe on the portal domain).
- All dashboard routes require a valid session cookie.

## Configuration

Key constants in `modal_app.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `OPENCODE_PORT` | `4096` | Port OpenCode listens on inside the sandbox |
| `SANDBOX_TIMEOUT` | `24h` | Max sandbox lifetime before auto-termination |
| `APP_NAME` | `openportal` | Modal app name |
| `PASSWORD_SECRET_NAME` | `opencode-secret` | Modal secret containing the password |
