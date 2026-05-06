#!/usr/bin/env bash
# Uninstall the tunneld launchd daemon installed by install-tunneld.sh.
# Safe to run when nothing is installed.
#
# Override via env vars (must match what was used at install time):
#   GPSSPOOF_LABEL     - launchd job label (default: com.gpsspoof.tunneld)
#   GPSSPOOF_PLIST_DIR - LaunchDaemons dir (default: /Library/LaunchDaemons)

set -euo pipefail

LABEL="${GPSSPOOF_LABEL:-com.gpsspoof.tunneld}"
PLIST_DIR="${GPSSPOOF_PLIST_DIR:-/Library/LaunchDaemons}"
PLIST="${PLIST_DIR}/${LABEL}.plist"

removed_anything=0

if sudo launchctl print "system/${LABEL}" >/dev/null 2>&1; then
    echo "stopping daemon..."
    sudo launchctl bootout "system/${LABEL}"
    removed_anything=1
fi

if [[ -f "$PLIST" ]]; then
    echo "removing plist: $PLIST"
    sudo rm "$PLIST"
    removed_anything=1
fi

if [[ "$removed_anything" -eq 0 ]]; then
    echo "tunneld daemon was not installed (nothing to remove)."
    exit 0
fi

echo
echo "tunneld daemon removed."
echo "gpsspoof now needs sudo for set/clear/ui (in-process tunnel fallback)."
