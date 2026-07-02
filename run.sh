#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GKLIB_DIR="${GKLIB_DIR:-"$ROOT_DIR/GKlib"}"
METIS_DIR="${METIS_DIR:-"$ROOT_DIR/METIS"}"

AIG_FILE="${1:-"${AIG_FILE:-"$ROOT_DIR/test_15_TOP32_72_expanded.aig"}"}"
PARTS="${2:-"${PARTS:-2}"}"

PREFIX="${PREFIX:-"$HOME/local"}"
CC="${CC:-gcc}"
SHARED="${SHARED:-1}"
OUT_DIR="${OUT_DIR:-"$ROOT_DIR/aig_partition_out_metis"}"

GP_METIS="$METIS_DIR/build/programs/gpmetis"
CUTPOINT_SCRIPT="$ROOT_DIR/scripts/partition_aig_cutpoints.py"

usage() {
  cat <<EOF
Usage:
  ./run.sh [aig_file] [parts]

Defaults:
  aig_file = $ROOT_DIR/test_15_TOP32_72.aig
  parts    = 2

Environment:
  PREFIX=/path/to/install-prefix   default: $HOME/local
  CC=gcc                           default: gcc
  SHARED=1                         default: 1
  OUT_DIR=/path/to/output-dir       default: $ROOT_DIR/aig_partition_out_metis
  GKLIB_DIR=/path/to/GKlib          default: $ROOT_DIR/GKlib
  METIS_DIR=/path/to/METIS          default: $ROOT_DIR/METIS
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

need_file() {
  local path="$1"
  local label="$2"
  if [[ ! -f "$path" ]]; then
    echo "Missing $label: $path" >&2
    exit 1
  fi
}

need_dir() {
  local path="$1"
  local label="$2"
  if [[ ! -d "$path" ]]; then
    echo "Missing $label: $path" >&2
    exit 1
  fi
}

need_cmd() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "Missing command: $name" >&2
    exit 1
  fi
}

step() {
  printf '\n==> %s\n' "$1"
}

need_dir "$GKLIB_DIR" "GKlib source directory"
need_dir "$METIS_DIR" "METIS source directory"
need_file "$AIG_FILE" "AIG input file"
need_file "$CUTPOINT_SCRIPT" "AIG partition script"
need_cmd make
need_cmd cmake
need_cmd python3

step "Build and install GKlib"
(
  cd "$GKLIB_DIR"
  make config cc="$CC" prefix="$PREFIX"
  make
  make install
)

step "Build and install METIS"
(
  cd "$METIS_DIR"
  make config shared="$SHARED" cc="$CC" prefix="$PREFIX" gklib_path="$PREFIX"
  make install
)

need_file "$GP_METIS" "built gpmetis executable"

step "Check gpmetis"
"$GP_METIS" -help >/dev/null
echo "gpmetis: $GP_METIS"

step "Partition AIG and report cut variables"
python3 "$CUTPOINT_SCRIPT" "$AIG_FILE" \
  --parts "$PARTS" \
  --gpmetis "$GP_METIS" \
  --out-dir "$OUT_DIR"

REPORT_DIR="$OUT_DIR/$(basename "${AIG_FILE%.*}")"
JSON_REPORT="$REPORT_DIR/$(basename "${AIG_FILE%.*}").cutpoints.json"
TEXT_REPORT="$REPORT_DIR/$(basename "${AIG_FILE%.*}").cutpoints.txt"
VISUALIZATION_FILE="$REPORT_DIR/$(basename "${AIG_FILE%.*}").visualization.html"

step "Output files"
echo "json_report: $JSON_REPORT"
echo "text_report: $TEXT_REPORT"
echo "visualization_file: $VISUALIZATION_FILE"
echo "vertex_map_file: $REPORT_DIR/$(basename "${AIG_FILE%.*}").metis.vertex_map.tsv"
echo "partition_file: $REPORT_DIR/$(basename "${AIG_FILE%.*}").metis.graph.part.$PARTS"
