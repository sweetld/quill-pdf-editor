#!/usr/bin/env bash
# Start Quill PDF Editor's MCP server (stdio) from the project-local virtualenv.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
exec "$DIR/.venv/bin/python" -m pdfeditor.agent "$@"
