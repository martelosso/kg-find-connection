#!/usr/bin/env bash
# Stream a Wikidata truthy N-Triples dump and keep only the lines the people graph needs:
#   * wdt:P31 (instance of) — to identify humans (Q5) and classify intermediate nodes
#   * the relationship allowlist (interpersonal + institutional, incl. P39 for the weak group)
#   * rdfs:label in English and Spanish
#
# Usage: scripts/extract_people_slice.sh <dump.nt.gz|filtered.nt> [OUT_DIR]
# Output: OUT_DIR/slice.nt   (default OUT_DIR: data/wikidata/slice)
#
# Pure text filtering (gzip/grep/awk): N-Triples is one statement per line, so no RDF parser is
# needed at this stage. Python (scripts/build_people_graph.py) parses and validates the result.
# NOTE: this property list is the *extraction* superset; ranking/eligibility policy lives in
# config/relationships.yaml (Milestone 2). Adding a property here requires re-running extraction.
set -euo pipefail

IN="$1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${2:-${ROOT}/data/wikidata/slice}"
PROPS=(P31 P26 P22 P25 P3373 P1066 P108 P69 P463 P39)

mkdir -p "$OUT_DIR"
PATTERNS="$(mktemp)"
trap 'rm -f "$PATTERNS"' EXIT
for p in "${PROPS[@]}"; do echo "<http://www.wikidata.org/prop/direct/${p}> "; done > "$PATTERNS"
echo '<http://www.w3.org/2000/01/rdf-schema#label> ' >> "$PATTERNS"

case "$IN" in
  *.gz) READ=(gzip -dc "$IN") ;;
  *)    READ=(cat "$IN") ;;
esac

start=$(date +%s)
"${READ[@]}" \
  | LC_ALL=C grep -F -f "$PATTERNS" \
  | LC_ALL=C awk 'index($0, "rdf-schema#label> ") == 0 || /"@(en|es) \.$/' \
  > "${OUT_DIR}/slice.nt"
end=$(date +%s)

lines=$(wc -l < "${OUT_DIR}/slice.nt")
printf '{"input": "%s", "lines": %s, "elapsed_s": %s, "properties": "%s"}\n' \
  "$(basename "$IN")" "$lines" "$((end - start))" "${PROPS[*]}" > "${OUT_DIR}/slice.meta.json"
cat "${OUT_DIR}/slice.meta.json"
