#!/usr/bin/env bash
# phantom-api — first-time dev setup
# Usage: ./setup.sh
#
# Sets up a local dev environment. Full end-to-end operation requires a
# Redpill API key + NowPayments merchant credentials.
set -euo pipefail

echo "=== phantom-api setup ==="
echo ""

# ─── OS detection ─────────────────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"

# ─── Prerequisites ────────────────────────────────────────────────────────────

check_cmd() {
    command -v "$1" >/dev/null 2>&1
}

if [ "$OS" = "Darwin" ]; then
    # macOS
    if ! check_cmd brew; then
        echo "Error: Homebrew is required on macOS."
        echo "Install from: https://brew.sh"
        exit 1
    fi

    echo "Platform: macOS ($ARCH)"

    if ! check_cmd python3.12; then
        echo "Installing Python 3.12 via Homebrew..."
        brew install python@3.12
    fi

    PYTHON=python3.12

    # sqlcipher C headers are required to build the sqlcipher3 wheel.
    if ! brew list sqlcipher >/dev/null 2>&1; then
        echo "Installing sqlcipher (required for sqlcipher3 Python wheel)..."
        brew install sqlcipher
    fi

    SQLCIPHER_PREFIX="$(brew --prefix sqlcipher)"
    export C_INCLUDE_PATH="${SQLCIPHER_PREFIX}/include"
    export LIBRARY_PATH="${SQLCIPHER_PREFIX}/lib"
    export LDFLAGS="-L${SQLCIPHER_PREFIX}/lib"
    export CPPFLAGS="-I${SQLCIPHER_PREFIX}/include"

else
    # Debian/Ubuntu (and similar)
    echo "Platform: Linux ($ARCH)"

    if ! check_cmd python3.12; then
        echo "Python 3.12 not found. Attempting to install..."
        if check_cmd apt-get; then
            # Try deadsnakes PPA on Ubuntu, fall back to distro package
            if check_cmd add-apt-repository; then
                sudo add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
                sudo apt-get update -q
            fi
            sudo apt-get install -y python3.12 python3.12-venv python3.12-dev
        else
            echo "Error: apt-get not found and python3.12 is missing."
            echo "Install Python 3.12 manually, then re-run setup.sh."
            exit 1
        fi
    fi

    PYTHON=python3.12

    # sqlcipher dev headers
    if ! check_cmd pkg-config || ! pkg-config --exists sqlcipher 2>/dev/null; then
        echo "Installing libsqlcipher-dev..."
        if check_cmd apt-get; then
            sudo apt-get install -y libsqlcipher-dev pkg-config build-essential
        else
            echo "WARN: Cannot install libsqlcipher-dev automatically."
            echo "      Install it manually, then re-run setup.sh."
        fi
    fi
fi

# ─── Python version check ─────────────────────────────────────────────────────
PY_VER=$("$PYTHON" --version 2>&1)
echo "Using: $PY_VER"

# ─── Virtual environment ──────────────────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    "$PYTHON" -m venv venv
fi

source venv/bin/activate

echo "Upgrading pip..."
pip install -U pip --quiet

# ─── Dependencies ─────────────────────────────────────────────────────────────
echo "Installing dependencies from requirements.txt..."
pip install -r requirements.txt

echo "Installing dev dependencies from requirements-dev.txt..."
pip install -r requirements-dev.txt

# ─── Environment ──────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "Created .env from .env.example"
    echo "  Edit .env and set at minimum:"
    echo "    REDPILL_API_KEY=sk-..."
    echo "    NP_API_KEY=...        (NowPayments)"
    echo "    NP_IPN_SECRET=...     (NowPayments dashboard)"
fi

# ─── Dev database ─────────────────────────────────────────────────────────────
mkdir -p data

if [ ! -f "data/phantom.db" ]; then
    echo "Initializing dev SQLCipher database (data/phantom.db)..."
    PHANTOM_DB_PASSPHRASE=devonly-replace-me \
        python -c "import asyncio, db; asyncio.run(db.init_db('data/phantom.db'))"
    echo "  Dev DB initialized with passphrase: devonly-replace-me"
    echo "  (This passphrase is for local dev only — never use it in production)"
fi

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your REDPILL_API_KEY and payment rail settings"
echo "  2. Activate venv:          source venv/bin/activate"
echo "  3. Export dev passphrase:  export PHANTOM_DB_PASSPHRASE=devonly-replace-me"
echo "  4. Start dev server:       PHANTOM_DEV=1 uvicorn main:app --reload"
echo "  5. Open:                   http://localhost:8000"
echo "  6. Run tests:              python -m pytest tests/ -v"
echo ""
echo "See CLAUDE.md for full command reference and architecture notes."
echo "See RUNBOOK.md for production deployment checklist."
