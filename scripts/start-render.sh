#!/usr/bin/env bash
# Render.com start script for Research Digest Agent.
# Pulls the latest digest .md files from the repo, then starts the web GUI.
set -euo pipefail

echo "=== Research Digest Agent – Render start ==="

# Pull latest digest markdown from the Git repository
echo "--- git pull ---"
if git pull 2>/dev/null; then
    echo "git pull completato."
else
    echo "git pull non disponibile o fallito (si prosegue lo stesso)."
fi

# Start the web application
echo "--- avvio gui.py ---"
exec python gui.py --host 0.0.0.0
