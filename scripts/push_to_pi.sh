#!/bin/bash
# Push local code changes to the RPI over SSH.
#
# Requires SSH key auth: ~/.ssh/phenohive_rpi must be authorized on the RPI.
# One-time setup: run setup_ssh_key.sh to install the key.
#
# Usage:
#   bash scripts/push_to_pi.sh            # push all changed files, restart if .py changed
#   bash scripts/push_to_pi.sh --all      # push entire project tree
#   bash scripts/push_to_pi.sh --no-restart

set -euo pipefail

RPI="root@100.78.224.28"
REMOTE="/opt/phenohive"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/phenohive_rpi}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no"

MODE="changed"    # changed | all
DO_RESTART=true

for arg in "$@"; do
  case "$arg" in
    --all)        MODE="all" ;;
    --no-restart) DO_RESTART=false ;;
  esac
done

# Verify connectivity
if ! $SSH "$RPI" "true" 2>/dev/null; then
  echo "ERROR: Cannot reach $RPI. Is Tailscale connected?"
  echo "  Tailscale RPI address: 100.78.224.28"
  exit 1
fi

push_file() {
  local src="$1"
  local dst="$2"
  cat "$src" | $SSH "$RPI" "cat > $dst"
  echo "  pushed  $dst"
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if [ "$MODE" = "all" ]; then
  echo "Pushing full project tree to $RPI:$REMOTE ..."
  # tar + pipe: no rsync/scp required, works from Windows Git Bash
  tar -czf - \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='data' \
    --exclude='logs' \
    --exclude='config.ini' \
    . | $SSH "$RPI" "cd $REMOTE && tar -xzf - && find scripts -name '*.sh' -exec sed -i 's/\r$//' {} \;"
  echo "  full sync complete"
  NEED_RESTART=true
else
  echo "Pushing changed files to $RPI:$REMOTE ..."
  NEED_RESTART=false

  # Get files changed vs HEAD (staged + unstaged + untracked that git knows about)
  CHANGED=$(git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null) || true

  if [ -z "$CHANGED" ]; then
    echo "  No changed files detected by git. Use --all to force a full sync."
    exit 0
  fi

  while IFS= read -r file; do
    [ -f "$file" ] || continue   # skip deleted files
    # Never overwrite production-only config files with local defaults
    case "$file" in config.ini|.env) echo "  skipped $file (production config — use --all only if intentional)"; continue ;; esac
    push_file "$file" "$REMOTE/$file"
    # Mark restart needed if any Python or HTML file changed
    case "$file" in *.py|*.html) NEED_RESTART=true ;; esac
  done <<< "$CHANGED"
fi

if $DO_RESTART && ${NEED_RESTART:-false}; then
  echo "Restarting phenohive service..."
  $SSH "$RPI" "systemctl restart phenohive && sleep 2 && systemctl is-active phenohive"
  echo "  service restarted and active"
else
  echo "  (no restart needed or --no-restart passed)"
fi

echo ""
echo "Done. Debug UI: http://100.78.224.28:8080"
