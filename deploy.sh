#!/bin/bash
set -euo pipefail
REMOTE="${POLYBOT_VPS:-polybot@vps}"
REMOTE_DIR="/opt/polybot"
echo "Deploying polybot..."
ssh "$REMOTE" "cd $REMOTE_DIR && git pull && uv sync && sudo systemctl restart polybot"
echo "Deployed. Checking status..."
sleep 3
ssh "$REMOTE" "sudo systemctl status polybot --no-pager -l | head -20"
echo "Done."
