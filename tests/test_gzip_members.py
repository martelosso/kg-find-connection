"""scripts/gzip_members.py: member-aligned ranges for parallel decompression of the dump."""

from __future__ import annotations

import gzip
import importlib.util
import random
import zlib
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "gzip_members", Path(__file__).resolve().parent.parent / "scripts" / "gzip_members.py")
gzip_members = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gzip_members)


def _ntriples(rng: random.Random, n: int) -> bytes:
    return b"".join(
        f"<http://www.wikidata.org/entity/Q{rng.randrange(10**8)}> "
        f"<http://www.wikidata.org/prop/direct/P{rng.randrange(2000)}> "
        f"\"{rng.random()}\"@en .\n".encode() for _ in range(n))


def _decompress_all(data: bytes) -> bytes:
    """Decompress a (possibly multi-member) gzip blob, failing on truncation."""
    out, buf = [], data
    while buf:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out.append(d.decompress(buf))
        if not d.eof:
            raise ValueError("truncated member")
        buf = d.unused_data
    return b"".join(out)


@pytest.mark.parametrize("parts", [1, 2, 3, 7, 50])
def test_ranges_are_member_aligned_and_cover_the_file(tmp_path, monkeypatch, parts):
    monkeypatch.setattr(gzip_members, "WINDOW", 4096)  # exercise multi-window scanning
    rng = random.Random(parts)
    members = [_ntriples(rng, rng.randrange(200, 2000)) for _ in range(12)]
    path = tmp_path / "dump.nt.gz"
    path.write_bytes(b"".join(gzip.compress(m, mtime=0) for m in members))

    ranges = gzip_members.ranges(path, parts)
    blob = path.read_bytes()
    assert ranges[0][0] == 0
    assert sum(length for _, length in ranges) == len(blob)
    assert all(a + la == b for (a, la), (b, _) in zip(ranges, ranges[1:]))
    assert len(ranges) <= parts
    # each range decompresses on its own; in order they reproduce the original stream
    pieces = [_decompress_all(blob[off:off + length]) for off, length in ranges]
    assert b"".join(pieces) == b"".join(members)


def test_single_member_file_falls_back_to_one_range(tmp_path):
    path = tmp_path / "one.nt.gz"
    path.write_bytes(gzip.compress(_ntriples(random.Random(0), 5000), mtime=0))
    assert gzip_members.ranges(path, 8) == [(0, path.stat().st_size)]


def test_misaligned_range_is_detected():
    """The safety net the extraction relies on: a cut inside a member cannot decompress cleanly."""
    blob = gzip.compress(_ntriples(random.Random(1), 3000), mtime=0)
    with pytest.raises((ValueError, zlib.error)):
        _decompress_all(blob[: len(blob) // 2])
    with pytest.raises((ValueError, zlib.error)):
        _decompress_all(blob[len(blob) // 2:])
