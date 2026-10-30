#!/usr/bin/env python3
"""Read GGUF headers to determine architecture + key dims.

GUFO is NOT a general-purpose GGUF loader: it supports a fixed set of
architectures (deepseek_v4_flash, qwen variants, minimax_h3, ...). A model whose
architecture is not implemented will not load no matter how small it is, so any
smoke-test model MUST be arch-checked first.
"""
import struct
import sys

TYPES = {
    0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2),
    4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<?", 1),
    10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8),
}


def read_string(f):
    n = struct.unpack("<Q", f.read(8))[0]
    return f.read(n).decode("utf-8", "replace")


def read_kv(f):
    key = read_string(f)
    t = struct.unpack("<I", f.read(4))[0]
    if t == 8:                                   # array
        at = struct.unpack("<I", f.read(4))[0]
        n = struct.unpack("<Q", f.read(8))[0]
        if at == 8:
            val = [read_string(f) for _ in range(n)]
        else:
            fmt, ln = TYPES[at]
            val = [struct.unpack(fmt, f.read(ln))[0] for _ in range(n)]
    elif t == 9:                                  # nested string
        val = read_string(f)
    else:
        fmt, ln = TYPES[t]
        val = struct.unpack(fmt, f.read(ln))[0]
    return key, val


def header(path):
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            return None
        ver = struct.unpack("<I", f.read(4))[0]
        n_tensors = struct.unpack("<Q", f.read(8))[0]
        n_kv = struct.unpack("<Q", f.read(8))[0]
        meta = dict(read_kv(f) for _ in range(n_kv))
    return ver, n_tensors, meta


def main(paths):
    for p in paths:
        try:
            ver, nt, m = header(p)
        except Exception as exc:  # noqa: BLE001
            print(f"{p}\n  ERROR: {exc}")
            continue
        arch = m.get("general.architecture", "?")
        blks = m.get(f"{arch}.block_count", "?")
        ctx = m.get(f"{arch}.context_length", "?")
        name = m.get("general.name", "?")
        print(f"{p.split('/')[-1]}")
        print(f"  architecture : {arch}")
        print(f"  name         : {name}")
        print(f"  layers       : {blks}    tensors: {nt}    train_ctx: {ctx}")
        print(f"  gguf version : {ver}")


if __name__ == "__main__":
    main(sys.argv[1:])
