#!/usr/bin/env bash
# One-time setup of the KumoSQL test runner on Linux or WSL (Ubuntu/Debian). Usage: bash install.sh [dir]
set -euo pipefail
DIR="${1:-$HOME/kumo-runner}"
BASE="${KUMO_RUNNER_BASE:-https://raw.githubusercontent.com/walterogozaly/KumoSQL/claude/project-thread-lm6mcq/dell-runner}"
if ! python3 -c 'import sys, venv; assert sys.version_info >= (3, 11)' 2>/dev/null; then
  sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip git build-essential
fi
command -v git >/dev/null || sudo apt-get install -y git
mkdir -p "$DIR"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for f in runner.py config.json; do
  [ -f "$DIR/$f" ] && [ "$f" = config.json ] && { echo "keeping your existing config.json"; continue; }
  if [ -f "$here/$f" ]; then cp "$here/$f" "$DIR/$f"; else curl -fsSL "$BASE/$f" -o "$DIR/$f"; fi
done
cd "$DIR"
git config --global credential.helper store 2>/dev/null || true
echo "Pushing a heartbeat to the results branch. When git asks, use your GitHub username and a fine-grained token"
echo "(this repo only, Contents: read and write). WSL can instead reuse the Windows credential manager:"
echo "  git config --global credential.helper '/mnt/c/Program\\ Files/Git/mingw64/bin/git-credential-manager.exe'"
python3 runner.py init
echo
echo "Start it in the background:  cd $DIR && nohup python3 runner.py run >> work-runner.out 2>&1 &"
echo "(WSL stops with Windows shutdown; to start it at log-on use a Windows scheduled task running: wsl -e bash -lc 'cd $DIR && python3 runner.py run')"
