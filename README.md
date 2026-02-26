# OpenPortal

A self-hosted portal for managing multiple [OpenCode](https://opencode.ai) AI coding agent instances on [Modal](https://modal.com).

## Features

- Password-protected web dashboard
- Create, start, stop, and delete sandboxes
- Configurable CPU, RAM, and GPU per sandbox
- Isolated workspaces on a shared persistent volume
- Filesystem snapshots preserve installed packages and configs across restarts
- Live CPU/RAM/GPU stats in the toolbar
- Voice-to-clipboard via Whisper transcription
- PWA -- installable on mobile home screens

## Setup

1. Install Modal CLI:

```
pip install modal
modal setup
```

2. Create Modal secrets:

```
modal secret create opencode-secret OPENCODE_SERVER_PASSWORD=<your-password>
modal secret create openai-secret OPENAI_API_KEY=<your-api-key>
```

The `opencode-secret` is required for portal login. The `openai-secret` is optional and enables voice-to-clipboard transcription via OpenAI Whisper.

3. Deploy:

```
modal deploy modal_app.py
```

For development with hot-reload:

```
modal serve modal_app.py
```
