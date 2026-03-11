#!/bin/bash

set -euo pipefail

cd "$(dirname "$0")"

echo "Starting Maramax..."
echo "This app needs microphone and Accessibility permissions on macOS."
echo "Open the overlay with Option+Space, toggle recording with Cmd+R, copy with Cmd+C, and close with Esc."
echo ""

if [ -d ".venv" ]; then
  source .venv/bin/activate
fi

PYTHONPATH="$(pwd)/src" python -m parakeet_dictation.main
