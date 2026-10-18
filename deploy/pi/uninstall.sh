#!/usr/bin/env bash
# Remove what install.sh created. Does not restore the old Hermes searxng (8888) / multi-search (9090) services; those were removed.
set -euo pipefail
systemctl --user disable --now search-hub.service searxng-hub.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/search-hub.service" "$HOME/.config/systemd/user/searxng-hub.service"
systemctl --user daemon-reload
echo "services removed. Files kept (delete by hand if you want): ~/search-hub ~/app/searxng-hub ~/.config/search-hub"
