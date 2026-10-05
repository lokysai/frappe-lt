#!/usr/bin/env bash
set -euo pipefail

# Extract Chrome into this job's temporary tree; never install it on the host.
chrome_root="$JOB_TEMP/chrome"
mkdir -p "$chrome_root/bin"
curl --fail --silent --show-error --location --retry 3 \
  --proto '=https' --proto-redir '=https' \
  https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
  --output "$chrome_root/chrome.deb"
dpkg-deb --extract "$chrome_root/chrome.deb" "$chrome_root"
rm "$chrome_root/chrome.deb"

export LD_LIBRARY_PATH="$chrome_root/opt/google/chrome${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
ldd "$chrome_root/opt/google/chrome/chrome" > "$chrome_root/libraries.txt"
if grep --fixed-strings 'not found' "$chrome_root/libraries.txt"; then
  printf '%s\n' 'Chrome system libraries are missing; provision the runner prerequisites.' >&2
  exit 1
fi

python - <<'PY'
import os
import shlex
from pathlib import Path

root = Path(os.environ["JOB_TEMP"]) / "chrome"
wrapper = root / "bin/google-chrome"
wrapper.write_text(
    "#!/bin/sh\nexec " + shlex.quote(str(root / "opt/google/chrome/chrome"))
    + ' "$@"\n'
)
wrapper.chmod(0o755)
(root / "bin/google-chrome-stable").symlink_to("google-chrome")
PY
printf '%s\n' "$chrome_root/bin" >> "$GITHUB_PATH"
printf 'LD_LIBRARY_PATH=%s\n' "$LD_LIBRARY_PATH" >> "$GITHUB_ENV"
"$chrome_root/bin/google-chrome" --version
