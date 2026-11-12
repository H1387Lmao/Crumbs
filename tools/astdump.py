#!/usr/bin/env python3
"""Prints the parse tree of a file in the same format as selfhost/astdump.cb, so the two parsers can be compared:
    python3 tools/astdump.py FILE"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from crumbsc.diag import Source, Diagnostics, Span
from crumbsc.parser import parse, N

SKIP = {"kind", "span", "toks", "eof", "src"}


def esc(b, prefix=""):
    out = [prefix, '"']
    for c in b:
        if 32 <= c < 127 and c not in (34, 92):
            out.append(chr(c))
        else:
            out.append("\\x%02x" % c)
    out.append('"')
    return "".join(out)


def sp(s):
    return f"@{s.line}:{s.col}+{s.length}"


def dump(v):
    if v is None:
        return "None"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return esc(v.encode("utf-8"))
    if isinstance(v, bytes):
        return esc(v, "b")
    if isinstance(v, float):
        return "f"
    if isinstance(v, Span):
        return sp(v)
    if isinstance(v, N):
        attrs = ",".join(f"{k}={dump(x)}" for k, x in sorted(v.__dict__.items()) if k not in SKIP)
        return f"{v.kind}{sp(v.span)}{{{attrs}}}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(dump(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(f"{dump(k)}:{dump(x)}" for k, x in v.items()) + "}"
    raise TypeError(type(v))


def main():
    path = sys.argv[1]
    src = Source(path, open(path, encoding="utf-8").read())
    d = Diagnostics()
    tree = parse(src, d)
    for e in d.items:
        if e.level == "error":
            print(f"error {e.span.line}:{e.span.col} {e.msg}")
    print(dump(tree))


main()
