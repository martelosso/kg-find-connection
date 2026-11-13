"""Split a multi-member gzip file into byte ranges that start on member boundaries.

Usage: python scripts/gzip_members.py <file.gz> <parts>
Prints one "<offset> <length>" line per range; the ranges are contiguous and cover the file.

The Wikidata dumps are concatenations of many gzip members (~58 MB each), so each range can
be decompressed independently (`gzip -dc` handles several members in a row) and the
decompressed outputs concatenated in order reproduce the whole stream byte for byte.

A cut point is the first position at or after an even split that looks like a member start:
gzip magic (1f 8b 08), decodes without error and yields N-Triples text. This check is only a
heuristic. Correctness comes from gzip itself: every member carries a CRC32 and its length,
so a range that starts or ends anywhere other than a real boundary fails to decompress
("unexpected end of file" or a CRC error), and the extraction script aborts on any non-zero
exit status. If the file has a single member, one range is printed (sequential fallback).
"""

from __future__ import annotations

import sys
import zlib
from pathlib import Path

MAGIC = b"\x1f\x8b\x08"
WINDOW = 8 << 20          # bytes scanned per read while looking for a candidate
PROBE = 1 << 18           # compressed bytes decoded to validate a candidate


def looks_like_member(fh, offset: int) -> bool:
    fh.seek(offset)
    chunk = fh.read(PROBE)
    try:
        out = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(chunk, 4096)
    except zlib.error:
        return False
    return out[:1] in (b"<", b"_") and b"\n" in out


def next_member(fh, start: int, size: int) -> int | None:
    pos = start
    while pos < size:
        fh.seek(pos)
        buf = fh.read(WINDOW + len(MAGIC) - 1)
        i = buf.find(MAGIC)
        while i != -1:
            if looks_like_member(fh, pos + i):
                return pos + i
            i = buf.find(MAGIC, i + 1)
        pos += WINDOW
    return None


def ranges(path: Path, parts: int) -> list[tuple[int, int]]:
    size = path.stat().st_size
    cuts = [0]
    with open(path, "rb") as fh:
        for k in range(1, max(1, parts)):
            target = max(size * k // parts, cuts[-1] + 1)
            found = next_member(fh, target, size)
            if found is None:
                break
            if found > cuts[-1]:
                cuts.append(found)
    cuts.append(size)
    return [(a, b - a) for a, b in zip(cuts, cuts[1:])]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    for offset, length in ranges(Path(argv[1]), int(argv[2])):
        print(offset, length)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
