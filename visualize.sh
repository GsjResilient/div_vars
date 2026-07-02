#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${ROOT_DIR}/scripts/visualize_aig_keys.py"

usage() {
  cat <<EOF
Usage:
  ./visualize.sh <aig_file> <key_variables_file> [output_html]

The key-variable file contains raw AIG variable IDs (literal / 2).
Blank lines and # comments are allowed.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 2 || $# -gt 3 ]]; then
  usage >&2
  exit 2
fi

if [[ ! -f "$1" ]]; then
  echo "Missing AIG input: $1" >&2
  exit 2
fi
if [[ ! -f "$2" ]]; then
  echo "Missing key-variable file: $2" >&2
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "Missing command: python3" >&2
  exit 2
fi

python3 "${SCRIPT}" "$@"
