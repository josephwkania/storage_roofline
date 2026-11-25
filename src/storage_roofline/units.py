"""Byte sizes: parsing "32G" style arguments and printing them."""

from __future__ import annotations

import re

_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def parse_size(text: str | int) -> int:
    """Bytes from "4096", "4K", "64M", "32G" or "1T" (binary units; a trailing
    "iB" or "B" is allowed)."""
    if isinstance(text, int):
        return text
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?\s*", str(text), re.I)
    if not match:
        raise ValueError(f"not a size: {text!r} (use e.g. 4K, 64M, 32G)")
    return int(float(match.group(1)) * _UNITS[match.group(2).upper()])


def fmt_size(nbytes: float) -> str:
    """Short binary size, e.g. 4 KiB, 64 MiB, 1.5 GiB."""
    for unit, scale in (("TiB", 1024 ** 4), ("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if nbytes >= scale:
            value = nbytes / scale
            return f"{value:.0f} {unit}" if value == int(value) else f"{value:.1f} {unit}"
    return f"{nbytes:.0f} B"


def fio_size(nbytes: int) -> str:
    """Size in a form fio accepts exactly (whole KiB/MiB/GiB where possible)."""
    for suffix, scale in (("g", 1024 ** 3), ("m", 1024 ** 2), ("k", 1024)):
        if nbytes % scale == 0:
            return f"{nbytes // scale}{suffix}"
    return str(nbytes)
