#!/usr/bin/env bash
# Launch Quill PDF Editor from the project-local virtualenv.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/.venv/bin/python" -m pdfeditor "$@"
