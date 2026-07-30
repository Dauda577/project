# MCP Server - local-mcp-v2

MCP server with 8 tools, Bearer-token auth, and OAuth 2.1 shim, using Streamable HTTP transport on `127.0.0.1:8000/mcp`.

## Setup

```powershell
cd mcp-server
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

- `ROOT_DIR` is set to `C:\Users\USER\OneDrive\Desktop\project` (change in `tools.py` if needed)
- `MCP_AUTH_TOKEN` is loaded from `.env`; if missing, a fresh one is generated and appended on first run

## Run

```powershell
python server.py
```

Server starts at `http://127.0.0.1:8000/mcp` (Streamable HTTP).

## OAuth Flow (for Claude custom connector UI)

1. **POST /register** — register a client (returns `client_id` + `client_secret`)
2. **GET /authorize** — renders a consent page with an "Access Token" password field
3. **POST /authorize** — validates the submitted token against `MCP_AUTH_TOKEN`, returns an auth code on success
4. **POST /token** — exchanges the auth code for the bearer token

Only `/mcp` requires the Bearer token. The OAuth endpoints are public by design.

## Tools

| Tool | Description |
|------|-------------|
| `ping` | Health check, returns `pong` |
| `list_dir` | List directory contents (restricted to ROOT_DIR) |
| `read_file` | Read file contents (denylist blocks secret/token/key/credential/password/.env) |
| `write_file` | Write/overwrite file (creates `.bak` backup; denylist applies) |
| `edit_file` | Replace `old_string` with `new_string` (exactly 1 match required; `.bak` backup) |
| `reload_tools` | Hot-reload the tools module via `importlib.reload`, no restart needed |
| `echo_test` | Echoes input back (for testing hot-reload) |
| `run_command` | Execute a shell command (30s timeout, returns stdout/stderr/exit_code) |
| `search_files` | Recursively search for a text pattern across files under ROOT_DIR |
| `git_status` | Read-only git status/log/diff-stat for a repo path |
| `git_diff` | Git diff for a specific file (read-only, capped at 8000 chars) |
| `system_info` | Disk space, RAM usage, CPU load |
| `process_status` | Shows running Python/node processes and checks port 8000 |
| `get_process_tree` | Parent/child process relationships by name or PID (via CIM/WMI) |
| `kill_process` | Kill a process by PID (refuses critical system processes) |
| `env_check` | Checks .gitignore/venv/node_modules presence, Python/Node versions, outdated pip packages |
| `log_tail` | Last N lines of a log file, with optional truncate-on-disk |
| `disk_usage_by_folder` | Top N largest immediate subfolders by size under a path |
| `project_log` | Append or read timestamped entries in `project_log.md` |

## Running as a Windows Service (NSSM)

The simplest way to keep the server running in the background:

1. Install NSSM: `winget install nssm` or download from https://nssm.cc
2. Create the service:
   ```powershell
   nssm install MCP-Server "C:\Users\USER\AppData\Local\Programs\Python\Python313\python.exe" "C:\Users\USER\OneDrive\Desktop\project\mcp-server\server.py"
   nssm set MCP-Server AppDirectory "C:\Users\USER\OneDrive\Desktop\project\mcp-server"
   nssm set MCP-Server AppStdout "C:\Users\USER\OneDrive\Desktop\project\mcp-server\stdout.log"
   nssm set MCP-Server AppStderr "C:\Users\USER\OneDrive\Desktop\project\mcp-server\stderr.log"
   nssm set MCP-Server Start SERVICE_AUTO_START
   nssm start MCP-Server
   ```
3. Manage: `nssm restart/stop/status MCP-Server`
4. Uninstall: `nssm remove MCP-Server confirm`

## Simple .bat + Task Scheduler (alternative)

Create `run_server.bat`:

```batch
@echo off
C:\Users\USER\AppData\Local\Programs\Python\Python313\python.exe C:\Users\USER\OneDrive\Desktop\project\mcp-server\server.py
```

Then create a Task Scheduler task:
- Trigger: "At startup" (or "At log on")
- Action: Start the .bat file
- Run whether user is logged on or not
