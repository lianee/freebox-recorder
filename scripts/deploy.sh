#!/bin/bash
# Deploy the working tree to a Freebox VM through install.sh (developer shortcut).
# install.sh refuses while a recording is running (a restart cuts it, 2026-10-17) and
# if a test fails on the VM. FORCE=1 deploys even during a recording.
# VM: ssh destination, default the alias "fbxrec" (~/.ssh/config: Host fbxrec / HostName <VM IP> / User freebox).
set -euo pipefail
VM=${VM:-fbxrec}
cd "$(dirname "$0")/.."
tar czf - --exclude=__pycache__ install.sh fbxrec tests systemd | ssh -o BatchMode=yes "$VM" "
set -e
d=\$(mktemp -d) && tar xzf - -C \$d
rc=0; sudo -n env FORCE=${FORCE:-0} bash \$d/install.sh < /dev/null || rc=\$?
sudo -n rm -rf \$d; exit \$rc"
