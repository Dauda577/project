import os
import re
import sys
import shutil
import asyncio
import subprocess
import importlib
import json
import sqlite3
import logging
import uuid
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("mcp-server")

ROOT_DIR = Path("C:\\Users\\USER\\OneDrive\\Desktop\\project").resolve()

DENY_PATTERNS = [
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"key", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"^\.env$", re.IGNORECASE),
]

_mcp_instance = None
_registered_tools = []


def _is_path_denied(filepath: Path) -> bool:
    name = filepath.name
    for pat in DENY_PATTERNS:
        if pat.search(name):
            return True
    return False


def _resolve_path(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        p = ROOT_DIR / p
    p = p.resolve()
    root_str = str(ROOT_DIR).lower().rstrip("\\")
    p_str = str(p).lower().rstrip("\\")
    if p_str != root_str and not p_str.startswith(root_str + "\\"):
        raise ValueError(f"Path is outside ROOT_DIR ({ROOT_DIR})")
    return p


def register_all(mcp):
    global _mcp_instance, _registered_tools
    _mcp_instance = mcp
    tool_names_before = set(_registered_tools)

    for name in list(_registered_tools):
        try:
            mcp.remove_tool(name)
        except Exception:
            pass
    _registered_tools.clear()

    @mcp.tool(name="ping", description="Simple health check. Returns a confirmation string.")
    async def ping() -> str:
        return "pong"

    _registered_tools.append("ping")

    @mcp.tool(
        name="list_dir",
        description="Lists directory contents. Restricted to paths under ROOT_DIR.",
    )
    async def list_dir(path: str = ".") -> str:
        resolved = _resolve_path(path)
        if not resolved.is_dir():
            return f"Error: not a directory: {resolved}"
        items = []
        for entry in sorted(resolved.iterdir()):
            suffix = "\\" if entry.is_dir() else ""
            items.append(f"{entry.name}{suffix}")
        return "\n".join(items) if items else "(empty)"

    _registered_tools.append("list_dir")

    @mcp.tool(
        name="read_file",
        description="Reads a file's contents. Restricted to paths under ROOT_DIR. Denylist blocks secret, token, key, credential, password, .env files.",
    )
    async def read_file(path: str) -> str:
        resolved = _resolve_path(path)
        if not resolved.is_file():
            return f"Error: not a file: {resolved}"
        if _is_path_denied(resolved):
            return f"Error: access denied to file: {resolved.name}"
        try:
            return resolved.read_text(encoding="utf-8")
        except Exception as e:
            return f"Error reading file: {e}"

    _registered_tools.append("read_file")

    @mcp.tool(
        name="write_file",
        description="Writes/overwrites a file under ROOT_DIR. Auto-creates .bak backup. Denylist applies.",
    )
    async def write_file(path: str, content: str) -> str:
        resolved = _resolve_path(path)
        if _is_path_denied(resolved):
            return f"Error: access denied to file: {resolved.name}"
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if resolved.exists():
            bak_path = resolved.with_suffix(resolved.suffix + ".bak")
            shutil.copy2(resolved, bak_path)
        resolved.write_text(content, encoding="utf-8")
        return f"Written {len(content)} bytes to {resolved}"

    _registered_tools.append("write_file")

    @mcp.tool(
        name="edit_file",
        description="Replaces old_string with new_string in a file. Requires exactly one match. Creates .bak backup.",
    )
    async def edit_file(path: str, old_string: str, new_string: str) -> str:
        resolved = _resolve_path(path)
        if _is_path_denied(resolved):
            return f"Error: access denied to file: {resolved.name}"
        if not resolved.is_file():
            return f"Error: file not found: {resolved}"
        content = resolved.read_text(encoding="utf-8")
        count = content.count(old_string)
        if count == 0:
            return f"Error: old_string not found in file (0 matches)"
        if count > 1:
            return f"Error: old_string found {count} times in file (expected exactly 1 match)"
        bak_path = resolved.with_suffix(resolved.suffix + ".bak")
        shutil.copy2(resolved, bak_path)
        new_content = content.replace(old_string, new_string)
        resolved.write_text(new_content, encoding="utf-8")
        return f"Replaced 1 occurrence in {resolved}"

    _registered_tools.append("edit_file")

    @mcp.tool(
        name="echo_test",
        description="Trivial tool that echoes input back. Used for testing hot-reload.",
    )
    async def echo_test(message: str = "") -> str:
        return f"echo: {message}"

    _registered_tools.append("echo_test")

    @mcp.tool(
        name="reload_tools",
        description="Hot-reloads the tools module without restarting the server. Clears tool cache and notifies clients.",
    )
    async def reload_tools() -> str:
        global _registered_tools
        mcp_instance = _mcp_instance
        if mcp_instance is None:
            return "Error: mcp instance not set"
        current_names = list(_registered_tools)
        for name in current_names:
            try:
                mcp_instance.remove_tool(name)
            except Exception:
                pass
        _registered_tools.clear()
        mod_name = "tools"
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
        import tools as reloaded_module
        reloaded_module.register_all(mcp_instance)

        try:
            sm = mcp_instance.session_manager
            if sm:
                sessions = getattr(sm, "sessions", {}) or getattr(sm, "_sessions", {})
                for session in sessions.values():
                    try:
                        await session.send_notification("notifications/tools/list_changed")
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning(f"Could not send list_changed notification: {exc}")

        return f"Reloaded tools module. {len(_registered_tools)} tools registered."

    _registered_tools.append("reload_tools")

    @mcp.tool(
        name="run_command",
        description="Executes an arbitrary shell command. Optional cwd defaults to ROOT_DIR. 30-second timeout. Returns stdout/stderr/exit_code (last 10000 chars each).",
    )
    async def run_command(command: str, cwd: str | None = None) -> str:
        work_dir = ROOT_DIR
        if cwd:
            resolved_cwd = _resolve_path(cwd)
            if not resolved_cwd.is_dir():
                return f"Error: cwd is not a directory: {resolved_cwd}"
            work_dir = resolved_cwd
        logger.info(f"run_command: command={command!r}, cwd={work_dir}")
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(work_dir),
                ),
                timeout=30.0,
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            stdout_text = ""
            stderr_text = "TIMEOUT: command exceeded 30 seconds"
            exit_code = -1
        except Exception as e:
            stdout_text = ""
            stderr_text = f"Error: {e}"
            exit_code = -1
        stdout_trunc = stdout_text[-10000:]
        stderr_trunc = stderr_text[-10000:]
        logger.info(f"run_command: exit_code={exit_code}, stdout_len={len(stdout_text)}, stderr_len={len(stderr_text)}")
        return json.dumps({
            "stdout": stdout_trunc,
            "stderr": stderr_trunc,
            "exit_code": exit_code,
        })

    _registered_tools.append("run_command")

    @mcp.tool(
        name="search_files",
        description="Recursively searches for a text pattern across files under ROOT_DIR. Skips binary files, .git, __pycache__, venv, node_modules. Returns matching file paths with line numbers (max 200 results).",
    )
    async def search_files(query: str, path: str = ".", case_sensitive: bool = False) -> str:
        resolved = _resolve_path(path)
        if not resolved.is_dir():
            return f"Error: not a directory: {resolved}"
        skip_dirs = {".git", "__pycache__", "venv", "node_modules", ".venv"}
        pattern = query if case_sensitive else query.lower()
        results = []
        max_results = 200
        for root, dirs, files in os.walk(resolved):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fname in files:
                fpath = Path(root) / fname
                if _is_path_denied(fpath):
                    continue
                try:
                    if fpath.stat().st_size > 2_000_000:
                        continue
                    text = fpath.read_text(encoding="utf-8")
                except Exception:
                    continue
                for i, line in enumerate(text.splitlines(), start=1):
                    haystack = line if case_sensitive else line.lower()
                    if pattern in haystack:
                        rel = fpath.relative_to(ROOT_DIR)
                        results.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(results) >= max_results:
                            break
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break
        if not results:
            return "No matches found."
        suffix = f"\n... (truncated at {max_results} results)" if len(results) >= max_results else ""
        return "\n".join(results) + suffix

    _registered_tools.append("search_files")

    @mcp.tool(
        name="process_status",
        description="Shows running Python/node processes and checks whether port 8000 (the MCP server) is in use. Windows-specific (tasklist/netstat).",
    )
    async def process_status() -> str:
        try:
            tasklist_proc = await asyncio.create_subprocess_shell(
                'tasklist /FI "IMAGENAME eq python.exe" /FI "IMAGENAME eq node.exe"',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            out, _ = await tasklist_proc.communicate()
            tasklist_out = out.decode("utf-8", errors="replace")
        except Exception as e:
            tasklist_out = f"Error running tasklist: {e}"

        try:
            netstat_proc = await asyncio.create_subprocess_shell(
                "netstat -ano | findstr :8000",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            out2, _ = await netstat_proc.communicate()
            netstat_out = out2.decode("utf-8", errors="replace").strip() or "(nothing listening on port 8000)"
        except Exception as e:
            netstat_out = f"Error running netstat: {e}"

        return f"--- Python/Node processes ---\n{tasklist_out}\n--- Port 8000 status ---\n{netstat_out}"

    _registered_tools.append("process_status")

    @mcp.tool(
        name="git_status",
        description="Read-only git helper for a repo under ROOT_DIR. Runs git status, recent log, and diff stat. Never commits, pushes, or modifies the repo.",
    )
    async def git_status(path: str = ".") -> str:
        resolved = _resolve_path(path)
        if not resolved.is_dir():
            return f"Error: not a directory: {resolved}"
        commands = {
            "status": "git status --short --branch",
            "recent log": "git log --oneline -10",
            "diff stat": "git diff --stat",
        }
        output_parts = []
        for label, cmd in commands.items():
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(resolved),
                )
                out, err = await proc.communicate()
                text = out.decode("utf-8", errors="replace").strip()
                err_text = err.decode("utf-8", errors="replace").strip()
                if not text and err_text:
                    text = f"(error) {err_text}"
                elif not text:
                    text = "(none)"
            except Exception as e:
                text = f"Error: {e}"
            output_parts.append(f"--- {label} ---\n{text}")
        return "\n\n".join(output_parts)

    _registered_tools.append("git_status")

    @mcp.tool(
        name="system_info",
        description="Reports disk space, RAM usage, and CPU load for the machine.",
    )
    async def system_info() -> str:
        lines = []
        try:
            total, used, free = shutil.disk_usage(str(ROOT_DIR.anchor or "C:\\"))
            lines.append(f"Disk ({ROOT_DIR.anchor}): {used // (1024**3)}GB used / {total // (1024**3)}GB total ({free // (1024**3)}GB free)")
        except Exception as e:
            lines.append(f"Disk info error: {e}")

        try:
            import psutil
            vm = psutil.virtual_memory()
            lines.append(f"RAM: {vm.used // (1024**2)}MB used / {vm.total // (1024**2)}MB total ({vm.percent}% used)")
            lines.append(f"CPU load: {psutil.cpu_percent(interval=0.5)}%")
        except ImportError:
            lines.append("RAM/CPU info unavailable (psutil not installed — run: pip install psutil)")
        except Exception as e:
            lines.append(f"RAM/CPU info error: {e}")

        return "\n".join(lines)

    _registered_tools.append("system_info")

    @mcp.tool(
        name="project_log",
        description="Appends a timestamped note to a local project journal (project_log.md under ROOT_DIR), or reads recent entries. Use action='write' with a note, or action='read' with an optional count of recent entries.",
    )
    async def project_log(action: str, note: str = "", count: int = 20) -> str:
        log_path = ROOT_DIR / "project_log.md"
        if action == "write":
            if not note.strip():
                return "Error: note is empty"
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            entry = f"\n## {timestamp}\n{note.strip()}\n"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(entry)
            return f"Logged entry at {timestamp}"
        elif action == "read":
            if not log_path.exists():
                return "(no log entries yet)"
            content = log_path.read_text(encoding="utf-8")
            entries = content.split("\n## ")
            entries = [e for e in entries if e.strip()]
            recent = entries[-count:]
            recent = ["## " + e if not e.startswith("## ") else e for e in recent]
            return "\n".join(recent) if recent else "(no log entries yet)"
        else:
            return "Error: action must be 'write' or 'read'"

    _registered_tools.append("project_log")

    CRITICAL_PROCESS_NAMES = {
        "system", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
        "lsass.exe", "smss.exe", "svchost.exe", "explorer.exe", "dwm.exe",
        "registry", "memory compression",
    }

    @mcp.tool(
        name="kill_process",
        description="Kills a process by PID (Windows taskkill /F). Refuses to kill critical system processes (kernel, csrss, lsass, services, explorer, dwm, svchost, etc). Use system_info/process list output to find PIDs first.",
    )
    async def kill_process(pid: int) -> str:
        if pid <= 0:
            return "Error: invalid PID"
        try:
            check_proc = await asyncio.create_subprocess_shell(
                f'tasklist /FI "PID eq {pid}" /FO CSV /NH',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            out, _ = await check_proc.communicate()
            line = out.decode("utf-8", errors="replace").strip()
            if not line or "No tasks" in line:
                return f"Error: no process found with PID {pid}"
            proc_name = line.split(",")[0].strip('"').lower()
        except Exception as e:
            return f"Error looking up PID {pid}: {e}"

        if proc_name in CRITICAL_PROCESS_NAMES:
            return f"Refused: PID {pid} is '{proc_name}', a critical system process."

        try:
            kill_proc = await asyncio.create_subprocess_shell(
                f"taskkill /PID {pid} /F",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            out, err = await kill_proc.communicate()
            result = out.decode("utf-8", errors="replace").strip() or err.decode("utf-8", errors="replace").strip()
            return result or f"Killed PID {pid} ({proc_name})"
        except Exception as e:
            return f"Error killing PID {pid}: {e}"

    _registered_tools.append("kill_process")

    @mcp.tool(
        name="env_check",
        description="Checks dev environment health under a path: presence of .gitignore/venv/node_modules, Python and Node versions, and outdated pip packages.",
    )
    async def env_check(path: str = ".") -> str:
        resolved = _resolve_path(path)
        if not resolved.is_dir():
            return f"Error: not a directory: {resolved}"
        lines = []

        gitignore = resolved / ".gitignore"
        lines.append(f".gitignore: {'present' if gitignore.is_file() else 'MISSING'}")
        lines.append(f"venv/: {'present' if (resolved / 'venv').is_dir() or (resolved / '.venv').is_dir() else 'not found'}")
        lines.append(f"node_modules/: {'present' if (resolved / 'node_modules').is_dir() else 'not found'}")

        for label, cmd in [("Python version", "python --version"), ("Node version", "node --version")]:
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(resolved),
                )
                out, err = await proc.communicate()
                text = (out.decode("utf-8", errors="replace") + err.decode("utf-8", errors="replace")).strip()
                lines.append(f"{label}: {text or '(not found)'}")
            except Exception as e:
                lines.append(f"{label}: error ({e})")

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    "pip list --outdated --format=columns",
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(resolved),
                ),
                timeout=25.0,
            )
            out, _ = await proc.communicate()
            outdated = out.decode("utf-8", errors="replace").strip()
            lines.append(f"--- Outdated pip packages ---\n{outdated or '(none, or pip not available)'}")
        except asyncio.TimeoutError:
            lines.append("Outdated pip packages: check timed out (25s)")
        except Exception as e:
            lines.append(f"Outdated pip packages: error ({e})")

        return "\n".join(lines)

    _registered_tools.append("env_check")

    @mcp.tool(
        name="log_tail",
        description="Returns the last N lines of a log file under ROOT_DIR. Optionally truncates the file on disk to just those last N lines (truncate=true) to stop unbounded growth.",
    )
    async def log_tail(path: str, lines: int = 50, truncate: bool = False) -> str:
        resolved = _resolve_path(path)
        if _is_path_denied(resolved):
            return f"Error: access denied to file: {resolved.name}"
        if not resolved.is_file():
            return f"Error: file not found: {resolved}"
        try:
            all_lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as e:
            return f"Error reading file: {e}"
        tail = all_lines[-lines:] if lines > 0 else all_lines
        result = "\n".join(tail) if tail else "(empty file)"

        if truncate and len(all_lines) > len(tail):
            bak_path = resolved.with_suffix(resolved.suffix + ".bak")
            shutil.copy2(resolved, bak_path)
            resolved.write_text("\n".join(tail) + ("\n" if tail else ""), encoding="utf-8")
            result += f"\n\n(truncated file from {len(all_lines)} to {len(tail)} lines, backup saved to {bak_path.name})"

        return result

    _registered_tools.append("log_tail")

    @mcp.tool(
        name="disk_usage_by_folder",
        description="Reports the top N largest immediate subfolders (by total size) under a given path within ROOT_DIR. Helps locate what's eating disk space.",
    )
    async def disk_usage_by_folder(path: str = ".", top: int = 10) -> str:
        resolved = _resolve_path(path)
        if not resolved.is_dir():
            return f"Error: not a directory: {resolved}"

        def dir_size(p: Path) -> int:
            total = 0
            try:
                for entry in p.rglob("*"):
                    if entry.is_file():
                        try:
                            total += entry.stat().st_size
                        except OSError:
                            pass
            except Exception:
                pass
            return total

        sizes = []
        try:
            for entry in resolved.iterdir():
                if entry.is_dir():
                    sizes.append((entry.name, dir_size(entry)))
                elif entry.is_file():
                    try:
                        sizes.append((entry.name + " (file)", entry.stat().st_size))
                    except OSError:
                        pass
        except Exception as e:
            return f"Error scanning {resolved}: {e}"

        sizes.sort(key=lambda x: x[1], reverse=True)
        top_n = sizes[:top]
        if not top_n:
            return "(empty directory)"

        def fmt(n: int) -> str:
            for unit in ["B", "KB", "MB", "GB"]:
                if n < 1024:
                    return f"{n:.1f}{unit}"
                n /= 1024
            return f"{n:.1f}TB"

        return "\n".join(f"{name}: {fmt(size)}" for name, size in top_n)

    _registered_tools.append("disk_usage_by_folder")

    @mcp.tool(
        name="git_diff",
        description="Shows the git diff for a specific file under a repo path (read-only). Output capped at 8000 characters.",
    )
    async def git_diff(file: str, path: str = ".") -> str:
        resolved = _resolve_path(path)
        if not resolved.is_dir():
            return f"Error: not a directory: {resolved}"
        try:
            proc = await asyncio.create_subprocess_shell(
                f'git diff -- "{file}"',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(resolved),
            )
            out, err = await proc.communicate()
            text = out.decode("utf-8", errors="replace").strip()
            err_text = err.decode("utf-8", errors="replace").strip()
            if not text and err_text:
                return f"(error) {err_text}"
            if not text:
                return f"(no diff for {file} — unchanged, staged, or untracked)"
            if len(text) > 8000:
                text = text[:8000] + "\n... (truncated at 8000 chars)"
            return text
        except Exception as e:
            return f"Error: {e}"

    _registered_tools.append("git_diff")

    @mcp.tool(
        name="get_process_tree",
        description="Shows parent/child relationships for processes by name or PID, using CIM/WMI data. Use before kill_process on anything ambiguous — reveals whether a process is a launcher stub for another (e.g. venv pythonw.exe redirectors) so you don't kill a load-bearing parent by mistake.",
    )
    async def get_process_tree(name: str = "", pid: int = 0) -> str:
        if not name and not pid:
            return "Error: provide either name or pid"
        ps_cmd = "Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name, CommandLine | ConvertTo-Json -Compress"
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    f'powershell -NoProfile -Command "{ps_cmd}"',
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                ),
                timeout=20.0,
            )
            out, err = await proc.communicate()
        except asyncio.TimeoutError:
            return "Error: powershell query timed out (20s)"
        except Exception as e:
            return f"Error running powershell: {e}"

        text = out.decode("utf-8", errors="replace").strip()
        if not text:
            err_text = err.decode("utf-8", errors="replace").strip()
            return f"Error: no process data returned. {err_text}"
        try:
            data = json.loads(text)
        except Exception as e:
            return f"Error parsing process data: {e}"
        if isinstance(data, dict):
            data = [data]

        by_pid = {p["ProcessId"]: p for p in data if p.get("ProcessId") is not None}
        children_map = {}
        for p in data:
            children_map.setdefault(p.get("ParentProcessId"), []).append(p)

        targets = []
        if pid:
            if pid in by_pid:
                targets.append(by_pid[pid])
        if name:
            name_lower = name.lower()
            for p in data:
                pname = (p.get("Name") or "").lower()
                if pname == name_lower or name_lower in pname:
                    targets.append(p)

        seen = set()
        uniq_targets = []
        for t in targets:
            tpid = t.get("ProcessId")
            if tpid is not None and tpid not in seen:
                uniq_targets.append(t)
                seen.add(tpid)

        if not uniq_targets:
            return "No matching processes found."

        lines = []
        for t in uniq_targets:
            tpid = t["ProcessId"]
            ppid = t.get("ParentProcessId")
            parent = by_pid.get(ppid)
            parent_desc = f'{parent.get("Name")} (PID {ppid})' if parent else f"PID {ppid} (not found / already exited)"
            kids = children_map.get(tpid, [])
            kid_desc = ", ".join(f'{k.get("Name")} (PID {k["ProcessId"]})' for k in kids) if kids else "(none)"
            cmdline = t.get("CommandLine") or "(none)"
            lines.append(
                f"PID {tpid} — {t.get('Name')}\n"
                f"  Parent: {parent_desc}\n"
                f"  Children: {kid_desc}\n"
                f"  CommandLine: {cmdline}"
            )
        return "\n\n".join(lines)

    _registered_tools.append("get_process_tree")

    def _tasks_db_path() -> Path:
        return ROOT_DIR / "tasks.db"

    def _get_db_conn() -> sqlite3.Connection:
        conn = sqlite3.connect(str(_tasks_db_path()))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS specs (
                id TEXT PRIMARY KEY,
                brief TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                spec_id TEXT NOT NULL,
                section TEXT NOT NULL,
                assigned_model TEXT NOT NULL,
                file_path TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                result TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        return conn

    @mcp.tool(
        name="create_spec",
        description="Creates a new build spec (a shared brief that multiple agents/tasks will work against). Returns a spec_id to use with enqueue_task, list_tasks, and get_spec.",
    )
    async def create_spec(brief: str) -> str:
        if not brief.strip():
            return "Error: brief is empty"
        spec_id = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc).isoformat()
        conn = _get_db_conn()
        try:
            conn.execute(
                "INSERT INTO specs (id, brief, created_at) VALUES (?, ?, ?)",
                (spec_id, brief.strip(), now),
            )
            conn.commit()
        finally:
            conn.close()
        return json.dumps({"spec_id": spec_id, "brief": brief.strip(), "created_at": now})

    _registered_tools.append("create_spec")

    @mcp.tool(
        name="enqueue_task",
        description="Adds a task under a spec_id, assigned to a specific section and model (e.g. section='header', assigned_model='deepseek'). Returns a task_id. Optionally set file_path for where the output should land.",
    )
    async def enqueue_task(spec_id: str, section: str, assigned_model: str, file_path: str = "") -> str:
        conn = _get_db_conn()
        try:
            spec_row = conn.execute("SELECT id FROM specs WHERE id = ?", (spec_id,)).fetchone()
            if not spec_row:
                return f"Error: no spec found with id {spec_id}"
            task_id = uuid.uuid4().hex[:8]
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO tasks (id, spec_id, section, assigned_model, file_path, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (task_id, spec_id, section, assigned_model, file_path, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return json.dumps({"task_id": task_id, "spec_id": spec_id, "section": section, "assigned_model": assigned_model, "status": "pending"})

    _registered_tools.append("enqueue_task")

    @mcp.tool(
        name="update_task",
        description="Updates a task's status ('pending', 'running', 'done', 'failed') and optionally its result text. Use this after a model finishes (or fails) its assigned section.",
    )
    async def update_task(task_id: str, status: str, result: str = "") -> str:
        valid_statuses = {"pending", "running", "done", "failed"}
        if status not in valid_statuses:
            return f"Error: status must be one of {sorted(valid_statuses)}"
        conn = _get_db_conn()
        try:
            row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return f"Error: no task found with id {task_id}"
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE tasks SET status = ?, result = ?, updated_at = ? WHERE id = ?",
                (status, result, now, task_id),
            )
            conn.commit()
        finally:
            conn.close()
        return f"Task {task_id} updated to status={status}"

    _registered_tools.append("update_task")

    @mcp.tool(
        name="list_tasks",
        description="Lists tasks, optionally filtered by spec_id and/or status. Returns id, section, assigned_model, status, file_path for each.",
    )
    async def list_tasks(spec_id: str = "", status: str = "") -> str:
        conn = _get_db_conn()
        try:
            query = "SELECT id, spec_id, section, assigned_model, status, file_path, updated_at FROM tasks WHERE 1=1"
            params = []
            if spec_id:
                query += " AND spec_id = ?"
                params.append(spec_id)
            if status:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY created_at ASC"
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        if not rows:
            return "(no matching tasks)"
        results = [
            {"task_id": r[0], "spec_id": r[1], "section": r[2], "assigned_model": r[3], "status": r[4], "file_path": r[5], "updated_at": r[6]}
            for r in rows
        ]
        return json.dumps(results, indent=2)

    _registered_tools.append("list_tasks")

    @mcp.tool(
        name="get_spec",
        description="Fetches a spec's brief plus a status summary of all its tasks (counts by status, and each task's current state).",
    )
    async def get_spec(spec_id: str) -> str:
        conn = _get_db_conn()
        try:
            spec_row = conn.execute("SELECT id, brief, created_at FROM specs WHERE id = ?", (spec_id,)).fetchone()
            if not spec_row:
                return f"Error: no spec found with id {spec_id}"
            task_rows = conn.execute(
                "SELECT id, section, assigned_model, status, file_path, result FROM tasks WHERE spec_id = ? ORDER BY created_at ASC",
                (spec_id,),
            ).fetchall()
        finally:
            conn.close()
        counts = {}
        tasks = []
        for r in task_rows:
            counts[r[3]] = counts.get(r[3], 0) + 1
            tasks.append({"task_id": r[0], "section": r[1], "assigned_model": r[2], "status": r[3], "file_path": r[4], "result": (r[5] or "")[:500]})
        return json.dumps({
            "spec_id": spec_row[0],
            "brief": spec_row[1],
            "created_at": spec_row[2],
            "status_counts": counts,
            "tasks": tasks,
        }, indent=2)

    _registered_tools.append("get_spec")

    async def _ask_opencode_impl(prompt: str, repo: str, model: str, timeout: float, tool_label: str) -> str:
        if not prompt.strip():
            return "Error: prompt is empty"
        repo_path = Path(os.path.expandvars(repo)).expanduser()
        if not repo_path.is_absolute():
            repo_path = (ROOT_DIR / repo_path).resolve()
        if not repo_path.is_dir():
            return f"Error: repo directory not found: {repo_path}"

        import tempfile
        fd, prompt_file = tempfile.mkstemp(suffix=".txt", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tf:
                tf.write(prompt)

            command = f'type "{prompt_file}" | opencode run --model {model} 2>&1'
            logger.info(f"{tool_label}: repo={repo_path}, model={model}, prompt_len={len(prompt)}")
            try:
                proc = await asyncio.wait_for(
                    asyncio.create_subprocess_shell(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=str(repo_path),
                    ),
                    timeout=timeout,
                )
                stdout_bytes, stderr_bytes = await proc.communicate()
                stdout_text = stdout_bytes.decode("utf-8", errors="replace")
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")
                exit_code = proc.returncode
            except asyncio.TimeoutError:
                stdout_text = ""
                stderr_text = f"TIMEOUT: opencode call exceeded {timeout} seconds"
                exit_code = -1
            except Exception as e:
                stdout_text = ""
                stderr_text = f"Error: {e}"
                exit_code = -1
        finally:
            try:
                os.remove(prompt_file)
            except Exception:
                pass

        logger.info(f"{tool_label}: exit_code={exit_code}, stdout_len={len(stdout_text)}")
        return json.dumps({
            "stdout": stdout_text[-10000:],
            "stderr": stderr_text[-4000:],
            "exit_code": exit_code,
        })

    @mcp.tool(
        name="ask_deepseek",
        description="Runs a prompt through DeepSeek via the OpenCode CLI (opencode run --model deepseek/deepseek-chat) inside a given repo directory. Use to delegate a coding task/section to DeepSeek as a subagent. repo can be any absolute path (not restricted to ROOT_DIR) or a path relative to ROOT_DIR. Returns stdout/stderr/exit_code. Default 120s timeout since opencode calls commonly take 30-60s+.",
    )
    async def ask_deepseek(prompt: str, repo: str, model: str = "deepseek/deepseek-chat", timeout: float = 120.0) -> str:
        return await _ask_opencode_impl(prompt, repo, model, timeout, "ask_deepseek")

    _registered_tools.append("ask_deepseek")

    @mcp.tool(
        name="ask_groq",
        description="Runs a prompt through Groq (LPU hardware, very low latency) via the OpenCode CLI inside a given repo directory. Default model: Llama 3.3 70B Versatile. KNOWN ISSUE as of 2026-07-28: OpenCode's agent overhead (~46K tokens per call) exceeds Groq's free-tier TPM cap (12,000) on every available free model, so calls currently fail with a 'Request too large' TPM error regardless of prompt size. Fix is enabling Groq's Developer tier (free, just add a card, 10x limits) at console.groq.com/settings/billing. repo can be absolute or relative to ROOT_DIR. Returns stdout/stderr/exit_code.",
    )
    async def ask_groq(prompt: str, repo: str, model: str = "groq/llama-3.3-70b-versatile", timeout: float = 120.0) -> str:
        return await _ask_opencode_impl(prompt, repo, model, timeout, "ask_groq")

    _registered_tools.append("ask_groq")

    @mcp.tool(
        name="ask_gemini",
        description="Runs a prompt through Google Gemini via the OpenCode CLI inside a given repo directory. Default model: gemini-flash-latest (1M token context, good for heavy data payloads; gemini-2.5-flash is deprecated for new accounts, do not use). Free tier resets daily and is rate-limited (~1,500 req/day on Flash). repo can be absolute or relative to ROOT_DIR. Returns stdout/stderr/exit_code.",
    )
    async def ask_gemini(prompt: str, repo: str, model: str = "google/gemini-flash-latest", timeout: float = 120.0) -> str:
        return await _ask_opencode_impl(prompt, repo, model, timeout, "ask_gemini")

    _registered_tools.append("ask_gemini")

    @mcp.tool(
        name="ask_openrouter",
        description="Runs a prompt through OpenRouter (multi-provider model aggregator) via the OpenCode CLI inside a given repo directory. Default model routes to OpenRouter's free pool (openrouter/openrouter/free) — pass a specific model string like 'openrouter/qwen/qwen3-coder' to target a particular model instead. repo can be absolute or relative to ROOT_DIR. Returns stdout/stderr/exit_code.",
    )
    async def ask_openrouter(prompt: str, repo: str, model: str = "openrouter/openrouter/free", timeout: float = 120.0) -> str:
        return await _ask_opencode_impl(prompt, repo, model, timeout, "ask_openrouter")

    _registered_tools.append("ask_openrouter")

    @mcp.tool(
        name="ask_cerebras",
        description="Runs a prompt through Cerebras (wafer-scale inference hardware, fastest available) via the OpenCode CLI inside a given repo directory. Default model: gpt-oss-120b. KNOWN ISSUE as of 2026-07-28: all available models (gpt-oss-120b, gemma-4-31b, zai-glm-4.7) return 'Payment required to access this resource' despite valid auth — check billing status at cloud.cerebras.ai before assuming this tool works. repo can be absolute or relative to ROOT_DIR. Returns stdout/stderr/exit_code.",
    )
    async def ask_cerebras(prompt: str, repo: str, model: str = "cerebras/gpt-oss-120b", timeout: float = 120.0) -> str:
        return await _ask_opencode_impl(prompt, repo, model, timeout, "ask_cerebras")

    _registered_tools.append("ask_cerebras")

    def _model_to_opencode(model: str) -> str:
        if "/" in model:
            return model
        mapping = {
            "deepseek": "deepseek/deepseek-chat",
            "groq": "groq/llama-3.3-70b-versatile",
            "gemini": "google/gemini-flash-latest",
            "google": "google/gemini-flash-latest",
            "openrouter": "openrouter/openrouter/free",
            "cerebras": "cerebras/gpt-oss-120b",
        }
        return mapping.get(model.lower(), model)

    async def _run_task_impl(task_id: str, repo: str) -> str:
        conn = _get_db_conn()
        try:
            row = conn.execute(
                "SELECT spec_id, section, assigned_model, file_path FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not row:
                return json.dumps({"task_id": task_id, "status": "error", "error": "task not found"})
            spec_id, section, assigned_model, file_path = row
            spec_row = conn.execute("SELECT brief FROM specs WHERE id = ?", (spec_id,)).fetchone()
            if not spec_row:
                return json.dumps({"task_id": task_id, "status": "error", "error": f"spec {spec_id} not found"})
            brief = spec_row[0]
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("UPDATE tasks SET status = 'running', updated_at = ? WHERE id = ?", (now, task_id))
            conn.commit()
        finally:
            conn.close()

        opencode_model = _model_to_opencode(assigned_model)
        prompt_parts = [
            f"Project brief: {brief}",
            f"Your task: implement the '{section}' section/part only. Keep changes scoped to this part.",
        ]
        if file_path:
            prompt_parts.append(f"Target file: {file_path}")
        prompt = "\n\n".join(prompt_parts)

        raw = await _ask_opencode_impl(prompt, repo, opencode_model, 120.0, "run_task")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"stdout": "", "stderr": raw, "exit_code": -1}
        exit_code = parsed.get("exit_code", -1)
        stdout = parsed.get("stdout", "") or ""
        stderr = parsed.get("stderr", "") or ""
        status = "done" if exit_code == 0 else "failed"
        result_text = (stdout.strip() or stderr.strip())[-2000:]

        conn = _get_db_conn()
        try:
            now2 = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE tasks SET status = ?, result = ?, updated_at = ? WHERE id = ?",
                (status, result_text, now2, task_id),
            )
            conn.commit()
        finally:
            conn.close()

        return json.dumps({
            "task_id": task_id,
            "section": section,
            "assigned_model": assigned_model,
            "status": status,
            "exit_code": exit_code,
            "result_preview": result_text[:500],
        })

    @mcp.tool(
        name="run_task",
        description="Runs a single pending task end-to-end: builds a prompt from its spec's brief + section, calls ask_deepseek against the given repo, and updates the task's status to 'done' or 'failed' with the result. Returns a summary. Task must already exist (created via enqueue_task).",
    )
    async def run_task(task_id: str, repo: str) -> str:
        return await _run_task_impl(task_id, repo)

    _registered_tools.append("run_task")

    @mcp.tool(
        name="run_spec",
        description="Runs every pending task for a spec sequentially through run_task (one at a time, to avoid concurrent opencode runs clobbering the same repo), updating each task's status as it completes. Returns a JSON array of per-task summaries. For fine-grained control or parallelization across repos, call run_task individually instead.",
    )
    async def run_spec(spec_id: str, repo: str) -> str:
        conn = _get_db_conn()
        try:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE spec_id = ? AND status = 'pending' ORDER BY created_at ASC",
                (spec_id,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return f"No pending tasks for spec {spec_id}"
        results = []
        for (task_id,) in rows:
            r = await _run_task_impl(task_id, repo)
            try:
                results.append(json.loads(r))
            except Exception:
                results.append({"task_id": task_id, "status": "error", "raw": r})
        return json.dumps(results, indent=2)

    _registered_tools.append("run_spec")

    @mcp.tool(
        name="web_search",
        description="Searches the web via the Brave Search API. Requires BRAVE_API_KEY set in mcp-server/.env (free tier: 2,000 queries/month, get a key at api-dashboard.search.brave.com). Returns up to `count` results (title, url, description) as JSON. Returns a clear error if the key isn't configured yet.",
    )
    async def web_search(query: str, count: int = 5) -> str:
        api_key = os.getenv("BRAVE_API_KEY")
        if not api_key:
            return "Error: BRAVE_API_KEY not set in mcp-server/.env. Get a free key at api-dashboard.search.brave.com, add it to .env yourself, then restart the server."
        if not query.strip():
            return "Error: query is empty"
        count = max(1, min(count, 10))

        import urllib.request
        import urllib.parse
        import urllib.error

        params = urllib.parse.urlencode({"q": query, "count": count})
        url = f"https://api.search.brave.com/res/v1/web/search?{params}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        })

        def _do_request():
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8")

        try:
            loop = asyncio.get_event_loop()
            body = await asyncio.wait_for(loop.run_in_executor(None, _do_request), timeout=20.0)
        except asyncio.TimeoutError:
            return "Error: Brave Search request timed out"
        except urllib.error.HTTPError as e:
            return f"Error: Brave Search API returned HTTP {e.code}: {e.reason}"
        except Exception as e:
            return f"Error calling Brave Search API: {e}"

        try:
            data = json.loads(body)
        except Exception as e:
            return f"Error parsing Brave Search response: {e}"

        results = []
        for item in (data.get("web", {}).get("results", []) or [])[:count]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
            })
        logger.info(f"web_search: query={query!r}, result_count={len(results)}")
        return json.dumps({"query": query, "results": results}, indent=2)

    _registered_tools.append("web_search")

    _CODE_DENY_PATTERNS = [
        re.compile(r"\bos\.system\b"),
        re.compile(r"\bsubprocess\b"),
        re.compile(r"\bsocket\b"),
        re.compile(r"\burllib\b"),
        re.compile(r"\brequests\b"),
        re.compile(r"\bctypes\b"),
        re.compile(r"__import__"),
        re.compile(r"\bshutil\b"),
        re.compile(r"\bos\.remove\b"),
        re.compile(r"\bos\.unlink\b"),
        re.compile(r"\bos\.rmdir\b"),
        re.compile(r"""open\s*\([^)]*['"][wWaAxX]"""),
        re.compile(r"\beval\s*\("),
        re.compile(r"\bexec\s*\("),
        re.compile(r"\bcompile\s*\("),
        re.compile(r"\bimportlib\b"),
        re.compile(r"\bpickle\b"),
    ]

    @mcp.tool(
        name="run_code_sandboxed",
        description="Executes a short Python snippet in a throwaway temp directory with a stripped environment (no access to .env secrets) and a timeout, then deletes the temp dir. IMPORTANT: this is NOT a real security sandbox — no Docker or WSL distro is installed on this machine, so there's no true process/filesystem isolation. It's a reduced-blast-radius tool: minimal env vars, isolated cwd, and a static denylist blocking obviously dangerous patterns (subprocess, os.system, socket, requests/urllib, ctypes, eval/exec, file writes, shutil, pickle, importlib) before the code even runs. Do not treat this as safe for adversarial or untrusted code — only for quick, self-contained calculations/logic. Python only. Returns stdout/stderr/exit_code.",
    )
    async def run_code_sandboxed(code: str, timeout: float = 15.0) -> str:
        if not code.strip():
            return "Error: code is empty"
        for pat in _CODE_DENY_PATTERNS:
            if pat.search(code):
                return f"Error: code blocked by denylist (matched pattern: {pat.pattern!r}). This tool only runs simple, self-contained snippets — no subprocess/network/file-write/eval access."

        import tempfile
        work_dir = tempfile.mkdtemp(prefix="sandbox_")
        script_path = Path(work_dir) / "snippet.py"
        try:
            script_path.write_text(code, encoding="utf-8")

            minimal_env = {
                "PATH": os.environ.get("PATH", ""),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                "TEMP": work_dir,
                "TMP": work_dir,
            }

            try:
                proc = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        sys.executable, str(script_path),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=work_dir,
                        env=minimal_env,
                    ),
                    timeout=timeout,
                )
                stdout_bytes, stderr_bytes = await proc.communicate()
                stdout_text = stdout_bytes.decode("utf-8", errors="replace")
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")
                exit_code = proc.returncode
            except asyncio.TimeoutError:
                stdout_text = ""
                stderr_text = f"TIMEOUT: code exceeded {timeout} seconds"
                exit_code = -1
            except Exception as e:
                stdout_text = ""
                stderr_text = f"Error: {e}"
                exit_code = -1
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        logger.info(f"run_code_sandboxed: exit_code={exit_code}, code_len={len(code)}")
        return json.dumps({
            "stdout": stdout_text[-5000:],
            "stderr": stderr_text[-3000:],
            "exit_code": exit_code,
        })

    _registered_tools.append("run_code_sandboxed")

    added = [n for n in _registered_tools if n not in tool_names_before]
    if added:
        logger.info(f"Registered tools: {', '.join(added)}")

    return _registered_tools
