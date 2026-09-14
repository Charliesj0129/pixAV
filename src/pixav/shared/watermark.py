"""Detect Sehuatang watermark strings disguised as BitTorrent info hashes.

Sehuatang publishes 40-hex strings that look like info hashes but are actually
an XOR-obfuscated contact address: the first byte is the key, the remaining 19
bytes are the ciphertext. Feeding one to a torrent client produces a torrent
that can never resolve metadata, which permanently occupies an active download
slot.

Two predicates, deliberately different in width:

* :func:`is_known_watermark` matches only the confirmed contact address. It
  gates destructive maintenance scripts, where a false positive deletes real
  rows.
* :func:`is_watermark_info_hash` matches any hash decoding to printable ASCII.
  It gates the torrent-client boundary, where a false positive only rejects one
  magnet. A genuine 20-byte info hash decodes to 19 printable bytes with
  probability ``(95/256)**19``, so the wider rule costs nothing in practice.
"""

from __future__ import annotations

KNOWN_WATERMARK = "sehuatang@gmail.com"

_INFO_HASH_BYTES = 20
_PRINTABLE_MIN = 0x20
_PRINTABLE_MAX = 0x7E


def decode_xor_watermark(info_hash: str) -> str | None:
    """Decode Sehuatang's key-byte-plus-XOR-text encoding when it is valid."""
    try:
        raw = bytes.fromhex(info_hash)
    except ValueError:
        return None
    if len(raw) != _INFO_HASH_BYTES:
        return None
    key = raw[0]
    decoded = bytes(byte ^ key for byte in raw[1:])
    try:
        return decoded.decode("ascii")
    except UnicodeDecodeError:
        return None


def is_known_watermark(info_hash: str) -> bool:
    """Match only the confirmed contact watermark, never generic printable data."""
    return decode_xor_watermark(info_hash) == KNOWN_WATERMARK


def is_watermark_info_hash(info_hash: str) -> bool:
    """Match any 40-hex string that XOR-decodes to entirely printable ASCII."""
    decoded = decode_xor_watermark(info_hash)
    if decoded is None:
        return False
    return all(_PRINTABLE_MIN <= ord(char) <= _PRINTABLE_MAX for char in decoded)
