# project

Local dev workspace containing:

- **`mcp-server`** — local MCP server (file ops, git, shell, system info, process management) for Claude/Grok
- **`udns-core`** — Unified Device Nervous System hub (LAN discovery + USB/LAN file transfer)
- **`control-panel`** — Tauri desktop UI (Devforge) including USB/LAN transfer views

## Structure

```
project/
├── mcp-server/       # MCP server (see mcp-server/README.md)
├── udns-core/        # UDNS hub + transfer (see udns-core/README.md)
├── control-panel/    # Tauri dashboard
└── project_log.md    # Running dev journal / decision log
```

## Access

The server runs locally on `127.0.0.1:8000` and is also reachable remotely via a Cloudflare tunnel at `mcp.sneakershub.site`, auto-started hidden on boot via `mcp-server/hidden-cloudflared.vbs`. The public endpoint is gated by OAuth 2.1 + Bearer token auth — see `mcp-server/README.md` for the full auth flow.

## Repo history note

This repo (`Dauda577/project`) is the single source of truth for this codebase. It previously had a nested, independently-tracked `.git` inside `mcp-server/` pointing at a separate repo (`Dauda577/Local-mcp`) — that duplication has been resolved; the nested repo was removed and the standalone GitHub repo deleted. All history now lives here.

## Quick start

- MCP tools: see `mcp-server/README.md`
- **UDNS / LAN file transfer between PCs:** see `udns-core/README.md`
  - On each PC run once: `udns-core.exe install` (auto-starts receiver at logon)
  - Then: Ethernet or same LAN → Control Panel **LAN (UDNS)** → Rescan → Send → peer Accept/Decline
