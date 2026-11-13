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
PROPS=(P31 P26 P22 P25 P3373 P40 P451 P1038 P1066 P802 P184 P185 P108 P69 P463 P54 P112 P161 P57 P58 P86 P175 P39 P19 P166 P102 P737)

mkdir -p "$OUT_DIR"
PATTERNS="$(mktemp)"
trap 'rm -f "$PATTERNS"' EXIT
for p in "${PROPS[@]}"; do echo "<http://www.wikidata.org/prop/direct/${p}> "; done > "$PATTERNS"
echo '<http://www.w3.org/2000/01/rdf-schema#label> ' >> "$PATTERNS"

# The per-line filter. Every line is judged on its own, so filtering byte ranges of the stream
# in parallel and concatenating the results in order gives exactly the sequential output.
filter() {
  LC_ALL=C grep -F -f "$PATTERNS" \
    | LC_ALL=C awk 'index($0, "rdf-schema#label> ") == 0 || /"@(en|es) \.$/'
}

# Parallelism: the Wikidata .gz is ~1,200 concatenated gzip members, and one gzip|grep|awk
# pipeline is bound by grep (~0.5 GB/s of text, ~29 min for the dump) while 16 cores sit idle.
# The file is cut at member boundaries (scripts/gzip_members.py) and JOBS pipelines run on the
# ranges. A cut that is not a real boundary makes gzip fail (truncated member / CRC), and any
# failure aborts the run. JOBS=1 (or a single-member file) runs the original sequential path.
JOBS="${JOBS:-$(( $(nproc) / 2 > 1 ? $(nproc) / 2 : 1 ))}"
TMP_OUT="${OUT_DIR}/slice.nt.partial"

start=$(date +%s)
case "$IN" in
  *.gz)
    mapfile -t RANGES < <(uv run --project "$ROOT" python "${ROOT}/scripts/gzip_members.py" "$IN" "$JOBS")
    [[ ${#RANGES[@]} -ge 1 ]] || { echo "could not split $IN" >&2; exit 1; }
    echo "decompressing ${#RANGES[@]} member-aligned range(s) in parallel" >&2
    pids=()
    for i in "${!RANGES[@]}"; do
      read -r off len <<< "${RANGES[$i]}"
      ( dd if="$IN" bs=4M iflag=skip_bytes,count_bytes skip="$off" count="$len" status=none \
          | gzip -dc | filter > "${TMP_OUT}.$(printf '%03d' "$i")" ) &
      pids+=($!)
    done
    failed=0
    for pid in "${pids[@]}"; do wait "$pid" || failed=1; done
    if [[ "$failed" -ne 0 ]]; then
      rm -f "${TMP_OUT}".*
      echo "a range failed to decompress or filter; nothing written" >&2
      exit 1
    fi
    : > "$TMP_OUT"
    for i in "${!RANGES[@]}"; do   # append in order, freeing each part as we go
      part="${TMP_OUT}.$(printf '%03d' "$i")"
      cat "$part" >> "$TMP_OUT" && rm -f "$part"
    done
    ;;
  *) filter < "$IN" > "$TMP_OUT" ;;
esac
mv -f "$TMP_OUT" "${OUT_DIR}/slice.nt"
end=$(date +%s)

lines=$(wc -l < "${OUT_DIR}/slice.nt")
printf '{"input": "%s", "lines": %s, "elapsed_s": %s, "jobs": %s, "properties": "%s"}\n' \
  "$(basename "$IN")" "$lines" "$((end - start))" "${JOBS}" "${PROPS[*]}" > "${OUT_DIR}/slice.meta.json"
cat "${OUT_DIR}/slice.meta.json"
