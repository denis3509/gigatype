#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
UUID=gigatype-status@denis3509.local
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"
install -d "$DEST"
for file in metadata.json extension.js stylesheet.css; do
  install -m 0644 "$ROOT/extensions/$UUID/$file" "$DEST/$file"
done
if gnome-extensions enable "$UUID"; then
  echo "GigaType Status enabled. If updating, reload GNOME Shell to load the new code."
else
  # New extensions may not be discovered until the next Shell session.
  /usr/bin/python3 - <<'PY'
from gi.repository import Gio
settings = Gio.Settings.new('org.gnome.shell')
uuid = 'gigatype-status@denis3509.local'
enabled = settings.get_strv('enabled-extensions')
if uuid not in enabled:
    if not settings.set_strv('enabled-extensions', [*enabled, uuid]):
        raise SystemExit('Could not enable GigaType Status in GNOME settings')
disabled = settings.get_strv('disabled-extensions')
if uuid in disabled:
    settings.set_strv('disabled-extensions', [item for item in disabled if item != uuid])
Gio.Settings.sync()
PY
  echo "Installed and enabled for the next Shell session. On X11: Alt+F2, r, Enter."
fi
