#!/bin/bash
# Secure Agents - One-Command Setup for macOS
#
# This script bootstraps the Python environment, then delegates to
# `secure-agents setup` which handles everything else (Ollama, email,
# credentials, model pulls) based on which agents you select.
#
# Usage:
#   bash setup.sh                          # set up all enabled agents
#   bash setup.sh nda_reviewer             # set up a specific agent
#   bash setup.sh --dry-run                # preview without changes
#
# All arguments are passed through to `secure-agents setup`.

set -e

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${BOLD}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo ""
echo "================================================="
echo "  Secure Agents - Bootstrap"
echo "================================================="
echo ""

# ── 1. Check macOS ──────────────────────────────────────
if [[ "$(uname)" != "Darwin" ]]; then
    fail "This setup script is designed for macOS."
fi

# ── 2. Ensure Homebrew ──────────────────────────────────
if ! command -v brew &> /dev/null; then
    info "Installing Homebrew (you may be prompted for your password)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Apple Silicon
    if [[ -f /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
fi
ok "Homebrew"

# ── 3. Ensure Python 3.11+ ─────────────────────────────
PYTHON_CMD=""
for cmd in python3.13 python3.12 python3.11 python3; do
    if command -v "$cmd" &> /dev/null; then
        version=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [[ "$major" -ge 3 ]] && [[ "$minor" -ge 11 ]]; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [[ -z "$PYTHON_CMD" ]]; then
    info "Installing Python 3.12..."
    brew install python@3.12
    PYTHON_CMD="python3.12"
fi
ok "Python: $($PYTHON_CMD --version 2>&1)"

# ── 4. Virtual environment + package install ────────────
if [[ ! -f "$VENV_DIR/bin/python" ]]; then
    info "Creating virtual environment..."
    $PYTHON_CMD -m venv "$VENV_DIR"
fi

if [[ ! -f "$VENV_DIR/bin/secure-agents" ]]; then
    info "Installing secure-agents..."
    "$VENV_DIR/bin/pip" install --upgrade pip --quiet 2>/dev/null
    "$VENV_DIR/bin/pip" install -e "$SCRIPT_DIR" --quiet 2>/dev/null
    ok "secure-agents installed"
else
    # Check if source has changed since last install (editable mode: just verify importable)
    if ! "$VENV_DIR/bin/python" -c "import secure_agents.setup" 2>/dev/null; then
        info "Updating secure-agents..."
        "$VENV_DIR/bin/pip" install -e "$SCRIPT_DIR" --quiet 2>/dev/null
        ok "secure-agents updated"
    else
        ok "secure-agents already installed"
    fi
fi

# ── 5. Hand off to the Python setup command ─────────────
echo ""
info "Handing off to secure-agents setup..."
echo ""
exec "$VENV_DIR/bin/secure-agents" setup -c "$SCRIPT_DIR/config.yaml" "$@"
