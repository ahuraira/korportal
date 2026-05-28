#!/usr/bin/env python3
"""
korportal — a tiny diagnostic MCP server.

Exposes a curated, allowlisted set of shell commands and file reads
over MCP/SSE. Designed to live on a server (typically a production
box) and be reachable only over Tailscale.

Three tools:
  - exec(cmd)              -> allowlisted shell command
  - list_allowed()         -> the current allowlist (for discovery)
  - read_file(path, lines) -> structured file read under allowed roots

Add new commands by editing allowlist.yaml. The server hot-reloads
on the next call when the file mtime changes; for an immediate
reload run `sudo systemctl reload korportal`.

Config via env vars (all optional):
  KORPORTAL_BIND_HOST    default 127.0.0.1
  KORPORTAL_BIND_PORT    default 7800
  KORPORTAL_ALLOWLIST    default ./allowlist.yaml
  KORPORTAL_AUDIT_LOG    default /var/log/korportal/audit.log
  KORPORTAL_WORK_DIR     default /srv/korcrm
  KORPORTAL_READ_ROOTS   default /srv/korcrm:/etc/caddy:/var/log
  KORPORTAL_TIMEOUT_SEC  default 30
  KORPORTAL_MAX_BYTES    default 262144 (256 KB)
  KORPORTAL_TRANSPORT    default sse  (also: streamable-http)
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
ALLOWLIST_PATH = Path(os.getenv("KORPORTAL_ALLOWLIST", str(ROOT / "allowlist.yaml")))
AUDIT_LOG_PATH = Path(os.getenv("KORPORTAL_AUDIT_LOG", "/var/log/korportal/audit.log"))
WORK_DIR = Path(os.getenv("KORPORTAL_WORK_DIR", "/srv/korcrm"))
COMMAND_TIMEOUT_SEC = int(os.getenv("KORPORTAL_TIMEOUT_SEC", "30"))
MAX_OUTPUT_BYTES = int(os.getenv("KORPORTAL_MAX_BYTES", str(256 * 1024)))

_DEFAULT_READ_ROOTS = "/srv/korcrm:/etc/caddy:/var/log"
READ_ROOTS: list[Path] = [
    Path(r).resolve()
    for r in os.getenv("KORPORTAL_READ_ROOTS", _DEFAULT_READ_ROOTS).split(":")
    if r
]


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _setup_audit_logger() -> logging.Logger:
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            AUDIT_LOG_PATH,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        )
    except PermissionError:
        # Fall back to stderr when the configured path isn't writable
        # (typical when running as a non-root local user).
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))

    log = logging.getLogger("korportal.audit")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.addHandler(handler)
    log.propagate = False
    return log


_audit_log = _setup_audit_logger()


def audit(event: str, **fields: Any) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        **fields,
    }
    _audit_log.info(json.dumps(record, default=str))


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

class Allowlist:
    """fnmatch-style command allowlist, hot-reloads on file mtime change."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: list[dict[str, Any]] = []
        self.mtime: float = 0.0
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"Allowlist not found at {self.path}")
        raw = yaml.safe_load(self.path.read_text()) or {}
        self.entries = raw.get("commands", []) or []
        self.mtime = self.path.stat().st_mtime
        audit("allowlist_reload", count=len(self.entries), path=str(self.path))

    def maybe_reload(self) -> None:
        try:
            current = self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if current > self.mtime:
            self.reload()

    def match(self, cmd: str) -> dict[str, Any] | None:
        self.maybe_reload()
        for entry in self.entries:
            for pattern in entry.get("patterns", []):
                if fnmatch.fnmatchcase(cmd, pattern):
                    return {**entry, "matched_pattern": pattern}
        return None

    def describe(self) -> list[dict[str, Any]]:
        self.maybe_reload()
        return [
            {
                "name": e.get("name", "(unnamed)"),
                "description": e.get("description", ""),
                "patterns": list(e.get("patterns", [])),
                "risk": e.get("risk", "read"),
            }
            for e in self.entries
        ]


allowlist = Allowlist(ALLOWLIST_PATH)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "korportal",
    host=os.getenv("KORPORTAL_BIND_HOST", "127.0.0.1"),
    port=int(os.getenv("KORPORTAL_BIND_PORT", "7800")),
)


@mcp.tool(name="exec")
def exec_command(cmd: str) -> dict[str, Any]:
    """
    Run a shell command on this host.

    The command must match a glob pattern in allowlist.yaml. Call
    `list_allowed` first if you're unsure what's available.

    Returns: {stdout, stderr, exit_code, duration_ms, truncated}.
    On allowlist miss, returns {error: 'command_not_allowed', message}.
    Output is capped at KORPORTAL_MAX_BYTES (default 256 KB) per stream;
    commands time out after KORPORTAL_TIMEOUT_SEC (default 30s).
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return {"error": "empty_command"}

    entry = allowlist.match(cmd)
    if entry is None:
        audit("exec_denied", cmd=cmd)
        return {
            "error": "command_not_allowed",
            "message": (
                f"Not in allowlist: {cmd!r}. "
                "Call `list_allowed` for the menu, or extend "
                "allowlist.yaml on the host."
            ),
        }

    cwd = str(WORK_DIR) if WORK_DIR.exists() else None
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SEC,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        audit("exec_timeout", cmd=cmd, matched=entry.get("name"))
        return {"error": "timeout", "timeout_sec": COMMAND_TIMEOUT_SEC}
    except Exception as e:
        audit("exec_error", cmd=cmd, matched=entry.get("name"), error=str(e))
        return {"error": "subprocess_error", "message": str(e)}

    elapsed_ms = int((time.monotonic() - start) * 1000)
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    truncated = len(stdout) > MAX_OUTPUT_BYTES or len(stderr) > MAX_OUTPUT_BYTES

    audit(
        "exec",
        cmd=cmd,
        matched=entry.get("name"),
        matched_pattern=entry.get("matched_pattern"),
        risk=entry.get("risk", "read"),
        exit_code=result.returncode,
        duration_ms=elapsed_ms,
        stdout_bytes=len(stdout),
        stderr_bytes=len(stderr),
    )

    return {
        "stdout": stdout[:MAX_OUTPUT_BYTES],
        "stderr": stderr[:MAX_OUTPUT_BYTES],
        "exit_code": result.returncode,
        "duration_ms": elapsed_ms,
        "truncated": truncated,
        "cwd": cwd,
    }


@mcp.tool()
def list_allowed() -> dict[str, Any]:
    """
    Return every command pattern this server will run.

    Call this once at session start to discover what's available
    before reaching for `exec`. The output is small enough to read
    in full — group your debugging plan around the commands listed.
    """
    entries = allowlist.describe()
    return {
        "count": len(entries),
        "allowlist_path": str(ALLOWLIST_PATH),
        "work_dir": str(WORK_DIR),
        "read_roots": [str(r) for r in READ_ROOTS],
        "timeout_sec": COMMAND_TIMEOUT_SEC,
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "commands": entries,
    }


@mcp.tool()
def read_file(path: str, max_lines: int = 500) -> dict[str, Any]:
    """
    Read up to `max_lines` lines from a text file on this host.

    The resolved path must sit under one of the configured read
    roots (KORPORTAL_READ_ROOTS, default /srv/korcrm, /etc/caddy,
    /var/log). Prefer this over `exec("cat ...")` — no shell
    escaping, structured output, and the path check rejects
    traversal attempts.
    """
    try:
        p = Path(path).resolve(strict=False)
    except Exception as e:
        return {"error": "invalid_path", "message": str(e)}

    if not any(
        str(p) == str(root) or str(p).startswith(f"{root}{os.sep}")
        for root in READ_ROOTS
    ):
        audit("read_denied", path=str(p))
        return {
            "error": "path_not_allowed",
            "allowed_roots": [str(r) for r in READ_ROOTS],
        }

    if not p.exists():
        return {"error": "not_found", "path": str(p)}
    if not p.is_file():
        return {"error": "not_a_file", "path": str(p)}

    try:
        text = p.read_text(errors="replace")
    except PermissionError:
        audit("read_denied", path=str(p), reason="permission")
        return {"error": "permission_denied", "path": str(p)}

    lines = text.splitlines()
    lines_returned = min(len(lines), max(1, max_lines))
    head = "\n".join(lines[:lines_returned])

    audit("read", path=str(p), lines=lines_returned, lines_total=len(lines))
    return {
        "path": str(p),
        "lines_returned": lines_returned,
        "lines_total": len(lines),
        "truncated": len(lines) > lines_returned,
        "content": head,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = os.getenv("KORPORTAL_TRANSPORT", "sse")
    print(
        f"korportal {transport} bound to "
        f"{mcp.settings.host}:{mcp.settings.port} "
        f"(allowlist: {len(allowlist.entries)} entries, "
        f"work_dir: {WORK_DIR})",
        file=sys.stderr,
    )
    mcp.run(transport=transport)
