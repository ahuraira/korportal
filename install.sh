#!/usr/bin/env bash
#
# korportal install script — run on the prod box, as root.
#
# Prerequisites:
#   - Tailscale installed AND `sudo tailscale up` has been run
#   - Python 3.11+
#   - A user account that is in the `docker` group (so it can talk
#     to /var/run/docker.sock for `docker compose` commands)
#
# Usage:
#   sudo ./install.sh
#   sudo KORPORTAL_USER=deploy ./install.sh    # explicit service user
#
# What it does:
#   1. Validates prereqs (tailscale running, python3, docker group)
#   2. Copies server.py + allowlist.yaml + requirements.txt to
#      /opt/korportal/
#   3. Creates a venv and installs deps
#   4. Prepares /var/log/korportal/
#   5. Renders systemd/korportal.service with your user + tailscale IP
#   6. systemctl daemon-reload + enable --now
#   7. Prints the MCP URL + the `claude mcp add` command for your laptop

set -euo pipefail

INSTALL_DIR=/opt/korportal
LOG_DIR=/var/log/korportal
SERVICE_NAME=korportal
SERVICE_USER="${KORPORTAL_USER:-${SUDO_USER:-$USER}}"

red()    { printf "\033[31m%s\033[0m\n" "$*" >&2; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
blue()   { printf "\033[34m%s\033[0m\n" "$*"; }

if [ "$EUID" -ne 0 ]; then
  red "Please run as root: sudo $0"
  exit 1
fi

if [ "$SERVICE_USER" = "root" ]; then
  red "Refusing to install with SERVICE_USER=root."
  red "Set KORPORTAL_USER=<deploy-user> or run via sudo from a non-root account."
  exit 1
fi

# ---------------------------------------------------------------------------
# Prereqs
# ---------------------------------------------------------------------------

blue "==> Checking prerequisites"

if ! command -v tailscale >/dev/null 2>&1; then
  red "tailscale is not installed. Install with:"
  red "    curl -fsSL https://tailscale.com/install.sh | sh"
  exit 1
fi

TAILNET_IP="$(tailscale ip -4 2>/dev/null | head -n1 | tr -d '[:space:]')"
if [ -z "$TAILNET_IP" ]; then
  red "Tailscale didn't return an IPv4 address."
  red "    Check:  tailscale status   then   tailscale ip -4"
  red "    Make sure 'sudo tailscale up' has been run on this box."
  exit 1
fi
if ! [[ "$TAILNET_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  red "tailscale ip -4 returned something unexpected: '$TAILNET_IP'"
  red "    Refusing to bind to a malformed address (would risk binding 0.0.0.0)."
  exit 1
fi
green "    Tailscale IP: $TAILNET_IP"

if ! command -v python3 >/dev/null 2>&1; then
  red "python3 is not installed."
  exit 1
fi
PYTHON_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PYTHON_MAJOR="$(python3 -c 'import sys; print(sys.version_info.major)')"
PYTHON_MINOR="$(python3 -c 'import sys; print(sys.version_info.minor)')"
if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]; }; then
  red "Python 3.11+ required, found $PYTHON_VERSION"
  exit 1
fi
green "    Python: $PYTHON_VERSION"

if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  red "python3-venv missing (ensurepip unavailable). Install with:"
  red "    sudo apt install -y python${PYTHON_VERSION}-venv"
  red "Then re-run this installer."
  exit 1
fi
green "    python3-venv: ok"

if ! getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
  red "Service user '$SERVICE_USER' does not exist."
  red "Set KORPORTAL_USER=<existing-user> and re-run."
  exit 1
fi
green "    Service user: $SERVICE_USER"

if ! id -nG "$SERVICE_USER" | grep -qw docker; then
  red "User '$SERVICE_USER' is not in the 'docker' group."
  red "Add with:"
  red "    sudo usermod -aG docker $SERVICE_USER"
  red "Then re-login (or 'newgrp docker') and re-run this script."
  exit 1
fi
green "    User in docker group: yes"

if ! command -v docker >/dev/null 2>&1; then
  yellow "WARNING: 'docker' CLI not found. korportal will still install, but"
  yellow "         most allowlist patterns assume 'docker compose' is available."
fi

# ---------------------------------------------------------------------------
# Source files
# ---------------------------------------------------------------------------

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

for f in server.py allowlist.yaml requirements.txt systemd/korportal.service; do
  if [ ! -f "$SOURCE_DIR/$f" ]; then
    red "Missing source file: $SOURCE_DIR/$f"
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

blue "==> Installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_USER" \
  "$SOURCE_DIR/server.py" "$INSTALL_DIR/server.py"
install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_USER" \
  "$SOURCE_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"

# Don't overwrite an edited allowlist on re-install
if [ -f "$INSTALL_DIR/allowlist.yaml" ]; then
  yellow "    allowlist.yaml already exists — keeping yours, dropping new one as allowlist.yaml.new"
  install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_USER" \
    "$SOURCE_DIR/allowlist.yaml" "$INSTALL_DIR/allowlist.yaml.new"
else
  install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_USER" \
    "$SOURCE_DIR/allowlist.yaml" "$INSTALL_DIR/allowlist.yaml"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

blue "==> Creating venv + installing deps"
# Recreate the venv if it's missing OR broken (no pip = failed previous run).
if [ ! -x "$INSTALL_DIR/.venv/bin/pip" ]; then
  if [ -d "$INSTALL_DIR/.venv" ]; then
    yellow "    Cleaning broken venv from previous run"
    rm -rf "$INSTALL_DIR/.venv"
  fi
  sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/.venv"
fi
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
green "    venv ready: $INSTALL_DIR/.venv"

blue "==> Preparing log dir at $LOG_DIR"
mkdir -p "$LOG_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR"
chmod 0750 "$LOG_DIR"

blue "==> Writing systemd unit"
sed -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
    -e "s|__TAILSCALE_IP__|$TAILNET_IP|g" \
    "$SOURCE_DIR/systemd/korportal.service" \
    > "/etc/systemd/system/${SERVICE_NAME}.service"

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

# Give it a moment to bind
sleep 2

if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  red "korportal failed to start. Recent logs:"
  journalctl -u "$SERVICE_NAME" -n 50 --no-pager >&2 || true
  exit 1
fi

# ---------------------------------------------------------------------------
# Final
# ---------------------------------------------------------------------------

TAILNET_NAME="$(
  tailscale status --self --json 2>/dev/null \
    | python3 -c 'import sys, json; d = json.load(sys.stdin); print(d["Self"]["DNSName"].rstrip("."))' \
    2>/dev/null || true
)"

if [ -z "$TAILNET_NAME" ]; then
  TAILNET_NAME="$TAILNET_IP"
fi

green ""
green "==> korportal is running"
echo
echo "  Bind:         http://$TAILNET_IP:7800/sse"
echo "  DNS:          http://$TAILNET_NAME:7800/sse"
echo "  Audit log:    $LOG_DIR/audit.log"
echo "  Service:      systemctl status $SERVICE_NAME"
echo "  Tail logs:    journalctl -u $SERVICE_NAME -f"
echo "  Hot-reload:   sudo systemctl reload $SERVICE_NAME"
echo "  Edit allow:   sudo -u $SERVICE_USER vi $INSTALL_DIR/allowlist.yaml"
echo
blue "On your laptop:"
echo
echo "  claude mcp add korportal \\"
echo "    --transport sse \\"
echo "    --url http://$TAILNET_NAME:7800/sse"
echo
blue "Then optionally auto-allow the read-only tools in"
blue "  .claude/settings.local.json:"
cat <<'EOF'

  {
    "permissions": {
      "allow": [
        "mcp__korportal__list_allowed",
        "mcp__korportal__read_file",
        "mcp__korportal__exec"
      ]
    }
  }

EOF
