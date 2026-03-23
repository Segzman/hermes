#!/usr/bin/env bash
# ============================================================
# Hermes Slate Assistant — EC2 Ubuntu setup script
#
# Run once on a fresh Ubuntu instance:
#   curl -sSL https://your-repo/deploy/setup_ec2.sh | bash
# Or after cloning:
#   bash deploy/setup_ec2.sh
# ============================================================
set -euo pipefail

HERMES_DIR="$HOME/hermes"
VENV_DIR="$HERMES_DIR/.venv"

echo "==> Updating apt packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3.11 python3.11-venv python3.11-dev \
    git curl wget unzip \
    xvfb \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxcb1 libxkbcommon0 \
    libx11-6 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2

echo "==> Setting up Python venv..."
python3.11 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo "==> Installing Python dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r "$HERMES_DIR/requirements.txt"

echo "==> Installing Playwright browsers (Chromium only)..."
playwright install chromium
playwright install-deps chromium

echo "==> Installing Hermes agent..."
if ! command -v hermes &>/dev/null; then
    curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
fi

echo "==> Installing Hermes slate-assistant skill..."
SKILL_DEST="$HOME/.hermes/skills/productivity/slate-assistant"
mkdir -p "$SKILL_DEST"
cp -r "$HERMES_DIR/skills/slate-assistant/." "$SKILL_DEST/"
echo "    Skill installed to $SKILL_DEST"

echo "==> Setting up .env..."
if [ ! -f "$HERMES_DIR/.env" ]; then
    cp "$HERMES_DIR/.env.example" "$HERMES_DIR/.env"
    echo ""
    echo "  ⚠️  Edit $HERMES_DIR/.env and fill in your credentials, then re-run"
    echo "     setup or continue with: bash deploy/setup_ec2.sh --skip-env-check"
    if [[ "${1:-}" != "--skip-env-check" ]]; then
        exit 0
    fi
fi

echo "==> Installing systemd service..."
SERVICE_SRC="$HERMES_DIR/deploy/slate-checker.service"
SERVICE_DEST="/etc/systemd/system/slate-checker.service"

# Substitute real paths into the service file
sed \
    -e "s|__HERMES_DIR__|$HERMES_DIR|g" \
    -e "s|__VENV_DIR__|$VENV_DIR|g" \
    -e "s|__USER__|$USER|g" \
    "$SERVICE_SRC" | sudo tee "$SERVICE_DEST" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable slate-checker
echo "    Service installed. Start with: sudo systemctl start slate-checker"

echo ""
echo "============================================================"
echo " Setup complete! Next steps:"
echo ""
echo "  1. Fill in your credentials:"
echo "     nano $HERMES_DIR/.env"
echo ""
echo "     Recommended on EC2: keep only non-secret config in .env and set:"
echo "       AWS_REGION=..."
echo "       HERMES_AWS_SSM_PATH=/hermes/prod"
echo "     or:"
echo "       HERMES_AWS_SECRET_ID=your-secret-id"
echo "     Then attach an IAM role to the instance so Hermes can read them."
echo ""
echo "  2. Authenticate with Slate (do this locally or with X11 forwarding):"
echo "     source $VENV_DIR/bin/activate"
echo "     cd $HERMES_DIR"
echo "     python -m slate.auth"
echo ""
echo "     On EC2 without a display, use xvfb-run:"
echo "     xvfb-run --auto-servernum python -m slate.auth"
echo "     (This opens a virtual display — use VNC/noVNC to see it)"
echo ""
echo "  3. Test the checker:"
echo "     python -m slate.checker --list"
echo ""
echo "  4. Start the background watcher:"
echo "     sudo systemctl start slate-checker"
echo "     sudo journalctl -u slate-checker -f   # watch logs"
echo ""
echo "  5. Set up Hermes gateway for Telegram chat:"
echo "     hermes gateway setup   # choose Telegram"
echo "     hermes gateway install # install as a service"
echo "============================================================"
