#!/usr/bin/env bash
# Download the pinned Obscura headless browser (Rust, no Chromium needed).
# Cached by the workflow, so this normally takes well under a second.
set -euo pipefail

VERSION="${OBSCURA_VERSION:-v0.2.7}"
DEST="${1:-bin}"
mkdir -p "$DEST"

if [ -x "$DEST/obscura" ]; then
  echo "obscura already present: $("$DEST/obscura" --version 2>/dev/null || echo ok)"
  exit 0
fi

arch="$(uname -m)"
case "$arch" in
  x86_64|amd64) asset="obscura-x86_64-linux-stealth.tar.gz" ;;
  aarch64|arm64) asset="obscura-aarch64-linux-stealth.tar.gz" ;;
  *) echo "unsupported arch $arch"; exit 0 ;;
esac

url="https://github.com/h4ckf0r0day/obscura/releases/download/${VERSION}/${asset}"
echo "downloading $url"
if ! curl -fsSL --retry 3 --max-time 300 -o /tmp/obscura.tar.gz "$url"; then
  echo "::warning::obscura download failed, browser tier disabled this run"
  exit 0
fi

tar xzf /tmp/obscura.tar.gz -C "$DEST"
chmod +x "$DEST/obscura" "$DEST/obscura-worker" 2>/dev/null || chmod +x "$DEST/obscura"
"$DEST/obscura" --version || echo "::warning::obscura binary present but not runnable"
