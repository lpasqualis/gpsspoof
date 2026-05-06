#!/usr/bin/env bash
# Install pymobiledevice3 `remote tunneld` as a launchd daemon so that
# `gpsspoof set/clear/ui` can run without sudo. macOS only.
#
# Idempotent: re-running replaces the existing installation.
# All paths auto-detect; override via env vars if needed:
#   GPSSPOOF_LABEL    - launchd job label (default: com.gpsspoof.tunneld)
#   GPSSPOOF_LOG      - log file path     (default: /var/log/<label>.log)
#   GPSSPOOF_PLIST_DIR - LaunchDaemons dir (default: /Library/LaunchDaemons)
#   GPSSPOOF_PMD3     - path to pymobiledevice3 binary (default: auto-detect)

set -euo pipefail

LABEL="${GPSSPOOF_LABEL:-com.gpsspoof.tunneld}"
PLIST_DIR="${GPSSPOOF_PLIST_DIR:-/Library/LaunchDaemons}"
PLIST="${PLIST_DIR}/${LABEL}.plist"
LOG="${GPSSPOOF_LOG:-/var/log/${LABEL}.log}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd -P)"

# --- locate the pymobiledevice3 binary ---------------------------------
locate_pmd3() {
    if [[ -n "${GPSSPOOF_PMD3:-}" ]]; then
        echo "$GPSSPOOF_PMD3"
        return
    fi
    # Prefer a sibling .venv/ if this script lives inside the project.
    local repo_venv="${SCRIPT_DIR}/../.venv/bin/pymobiledevice3"
    if [[ -x "$repo_venv" ]]; then
        local resolved_dir
        resolved_dir="$(cd "$(dirname "$repo_venv")" && pwd -P)"
        echo "${resolved_dir}/pymobiledevice3"
        return
    fi
    if command -v pymobiledevice3 >/dev/null 2>&1; then
        command -v pymobiledevice3
        return
    fi
    return 1
}

if ! PMD3="$(locate_pmd3)"; then
    cat >&2 <<EOF
error: cannot find the pymobiledevice3 binary

Tried, in order:
  1. \$GPSSPOOF_PMD3 (not set)
  2. ${SCRIPT_DIR}/../.venv/bin/pymobiledevice3
  3. command -v pymobiledevice3

Install pymobiledevice3 (e.g. \`pip install -e .\` from repo root)
or set GPSSPOOF_PMD3 to its full path and re-run.
EOF
    exit 1
fi

if [[ ! -x "$PMD3" ]]; then
    echo "error: $PMD3 is not executable" >&2
    exit 1
fi

# --- summary -----------------------------------------------------------
cat <<EOF
installing tunneld daemon
  label:   $LABEL
  binary:  $PMD3
  plist:   $PLIST
  log:     $LOG
EOF
echo

# --- write plist + load -----------------------------------------------
# Bootout any existing instance so this is idempotent.
if sudo launchctl print "system/${LABEL}" >/dev/null 2>&1; then
    echo "  removing existing daemon (will reinstall)..."
    sudo launchctl bootout "system/${LABEL}" 2>/dev/null || true
fi

# Heredoc-write the plist. Using a quoted heredoc would block variable
# expansion; we want $LABEL/$PMD3/$LOG expanded and nothing else.
sudo tee "$PLIST" >/dev/null <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PMD3}</string>
        <string>remote</string>
        <string>tunneld</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>${LOG}</string>
    <key>StandardErrorPath</key>
    <string>${LOG}</string>
</dict>
</plist>
EOF

sudo chown root:wheel "$PLIST"
sudo chmod 644 "$PLIST"

echo "  bootstrapping into launchd..."
sudo launchctl bootstrap system "$PLIST"

# --- verify it's listening --------------------------------------------
echo "  waiting for tunneld to listen on 127.0.0.1:49151..."
for i in $(seq 1 20); do
    if nc -z 127.0.0.1 49151 2>/dev/null; then
        echo
        echo "tunneld is up. Test it:"
        echo "  gpsspoof set seattle    # should run without sudo"
        echo
        echo "Logs: sudo tail -f $LOG"
        exit 0
    fi
    sleep 0.5
done

cat >&2 <<EOF

warning: tunneld did not become reachable within 10s
         the daemon is loaded, but something failed during start.
         check log: sudo tail $LOG
EOF
exit 1
