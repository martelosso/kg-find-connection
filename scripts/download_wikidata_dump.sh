#!/usr/bin/env bash
# Download a dated Wikidata "truthy" N-Triples dump with resume + MD5 verification.
#
# Usage: scripts/download_wikidata_dump.sh [DUMP_DATE]     (default: 20261104)
# Output: data/wikidata/raw/wikidata-<DATE>-truthy-BETA.nt.gz   (gitignored, ~72 GB)
#
# Safe to re-run: curl resumes a partial file (-C -); a completed file is only re-verified.
#
# Wikimedia keeps only recent dumps. If the date is gone, pick a newer one from
# https://dumps.wikimedia.org/wikidatawiki/entities/ (a folder named YYYYMMDD that contains
# wikidata-YYYYMMDD-truthy-BETA.nt.gz) and pass it as DUMP_DATE.
# Runs on Linux, WSL, Git Bash (Windows) and macOS (needs curl; md5sum or md5).
set -euo pipefail

DATE="${1:-20261104}"
NAME="wikidata-${DATE}-truthy-BETA.nt.gz"
BASE="https://dumps.wikimedia.org/wikidatawiki/entities/${DATE}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${ROOT}/data/wikidata/raw"
OUT="${OUT_DIR}/${NAME}"
UA="${WIKIDATA_USER_AGENT:-knowledge-graph-workshop/0.1 (dump download)}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-30}"

mkdir -p "$OUT_DIR"

file_size() { wc -c < "$1" 2>/dev/null | tr -d ' ' || echo 0; }
md5_of() { if command -v md5sum >/dev/null; then md5sum "$1" | awk '{print $1}'; else md5 -q "$1"; fi; }

expected_md5="$(curl -fsS -A "$UA" "${BASE}/wikidata-${DATE}-md5sums.txt" | awk -v n="$NAME" '$2==n {print $1}' || true)"
expected_size="$(curl -fsSI -A "$UA" "${BASE}/${NAME}" | tr -d '\r' | awk 'tolower($1)=="content-length:" {print $2}' || true)"
if [[ -z "$expected_md5" || -z "$expected_size" ]]; then
  echo "could not read md5/size for $NAME: the ${DATE} dump is probably no longer online." >&2
  echo "Pick a newer date at https://dumps.wikimedia.org/wikidatawiki/entities/ and run: $0 YYYYMMDD" >&2
  exit 1
fi
echo "target: $NAME  size=${expected_size}  md5=${expected_md5}"

attempt=0
while :; do
  have=$( [[ -f "$OUT" ]] && file_size "$OUT" || echo 0 )
  [[ "$have" -ge "$expected_size" ]] && break
  attempt=$((attempt + 1))
  [[ "$attempt" -le "$MAX_ATTEMPTS" ]] || { echo "giving up after $MAX_ATTEMPTS attempts" >&2; exit 1; }
  echo "$(date '+%F %T') attempt ${attempt}: have ${have}/${expected_size} bytes"
  curl -fS -A "$UA" -C - --retry 5 --retry-delay 30 --connect-timeout 30 \
       --speed-limit 50000 --speed-time 120 \
       -o "$OUT" "${BASE}/${NAME}" || { echo "curl exited $?; retrying in 60 s"; sleep 60; }
done

echo "$(date '+%F %T') download complete; verifying md5 ..."
actual_md5="$(md5_of "$OUT")"
if [[ "$actual_md5" != "$expected_md5" ]]; then
  echo "MD5 MISMATCH: expected $expected_md5 got $actual_md5" >&2
  exit 2
fi
echo "OK $OUT"
printf '{"file": "%s", "dump_date": "%s", "size": %s, "md5": "%s", "source": "%s"}\n' \
  "$NAME" "$DATE" "$expected_size" "$expected_md5" "${BASE}/${NAME}" > "${OUT}.meta.json"
