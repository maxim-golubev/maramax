#!/bin/bash

set -euo pipefail

cd "$(dirname "$0")"

echo "Starting Maramax..."
echo "This app needs microphone and Accessibility permissions on macOS."
echo "Press Option+Space to start dictating and again (or Cmd+R) to finish."
echo "The transcript is copied to the clipboard. Use the menu bar icon for Settings, Recordings, and the full transcript window."
echo ""

if [ -d ".venv" ]; then
  source .venv/bin/activate
fi

PYTHONPATH="$(pwd)/src" python -m parakeet_dictation.main
