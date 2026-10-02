#!/bin/sh
set -eu
# Run once as the runtime user. sudo asks in the terminal; no password is stored.
test "$(uname -s):$(uname -m)" = Linux:x86_64
sudo apt-get update
sudo apt-get install -y bubblewrap xvfb openbox xauth x11-utils dbus-x11 curl unzip fonts-liberation libnss3 libatk-bridge2.0-0 libxkbcommon0 libgbm1 libasound2t64 libcups2t64 libxcomposite1 libxdamage1 libxrandr2 libgtk-3-0t64
tools="$HOME/.local/share/loginom-multica-tools"
mkdir -p "$tools" "$HOME/.local/bin"
if ! "$HOME/.local/bin/bun" --version 2>/dev/null | awk '$0 == "1.3.14" {ok=1} END {exit !ok}'; then
  curl -fL https://github.com/oven-sh/bun/releases/download/bun-v1.3.14/bun-linux-x64.zip -o "$tools/bun.zip"
  unzip -o "$tools/bun.zip" -d "$tools"
  install -m 755 "$tools/bun-linux-x64/bun" "$HOME/.local/bin/bun"
  rm "$tools/bun.zip"
fi
if [ ! -x "$tools/node-v24.19.0-linux-x64/bin/node" ]; then
  curl -fL https://nodejs.org/dist/v24.19.0/node-v24.19.0-linux-x64.tar.xz -o "$tools/node.tar.xz"
  tar -xJf "$tools/node.tar.xz" -C "$tools"
  rm "$tools/node.tar.xz"
fi
printf '%s  %s\n' bc17c508ffeed0ec622934f9b7fa72f8e78da65350e63c3eceb56fa688aa5e12 "$tools/node-v24.19.0-linux-x64/bin/node" | sha256sum -c -
export PATH="$tools/node-v24.19.0-linux-x64/bin:$HOME/.local/bin:$PATH"
mkdir -p "$tools/account-ui"
npm install --prefix "$tools/account-ui" --ignore-scripts --no-audit --fund=false playwright@1.63.0-alpha-2026-08-31
PLAYWRIGHT_BROWSERS_PATH="$tools/browsers" node "$tools/account-ui/node_modules/playwright/cli.js" install chromium
printf '%s  %s\n' 8c599d43aec53f2460a31ae2f4af6bd863f8258b34ff519564bc5d4726bfaa1e "$tools/browsers/chromium-1243/chrome-linux64/chrome" | sha256sum -c -
if [ -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns ] && [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns)" = 1 ]; then
  sudo install -m 644 "$(dirname "$0")/loginom-multica-bwrap.apparmor" /etc/apparmor.d/loginom-multica-bwrap
  sudo apparmor_parser -r /etc/apparmor.d/loginom-multica-bwrap
fi
/usr/bin/bwrap --unshare-all --share-net --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64 --proc /proc --dev /dev -- /usr/bin/true
"$HOME/.local/bin/bun" --revision
node --version
