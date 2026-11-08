#!/usr/bin/env python3
"""Runs the whole checker on a program and prints the annotated syntax trees and the diagnostics, in the format of
selfhost/ckdump.cb:  python3 tools/ckdump.py FILE [--nostd] [--std=DIR]"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from crumbsc.diag import Diagnostics, CompileError, Span
from crumbsc.loader import Loader
from crumbsc.checker import Checker, Local
from crumbsc.codegen import C_RESERVED
from crumbsc.parser import N
from crumbsc.types import Ty
from crumbsc.checker import Sym
from crumbsc.fmtspec import Spec

SKIP = {"kind", "span", "toks", "eof"}


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
    if isinstance(v, Spec):
        return f"Spec({v.fill},{v.align},{v.sign},{int(v.alt)},{int(v.zero)},{v.width},{v.group},{v.prec},{v.type})"
    if isinstance(v, Ty):
        return f"T({v})"
    if isinstance(v, Sym):
        return f"S({v.full})"
    if isinstance(v, Local):
        return f"L({v.name}@{v.span.line}:{v.span.col})"
    if isinstance(v, N):
        attrs = ",".join(f"{k}={dump(x)}" for k, x in sorted(v.__dict__.items()) if k not in SKIP and not (k == "src" and type(x).__name__ == "Source"))
        return f"{v.kind}{sp(v.span)}{{{attrs}}}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(dump(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(f"{dump(k)}:{dump(x)}" for k, x in v.items()) + "}"
    return "?" + type(v).__name__


def main():
    path = sys.argv[1]
    nostd = False
    include = ()
    for a in sys.argv[2:]:
        if a == "--nostd":
            nostd = True
        elif a.startswith("--std="):
            pass
    d = Diagnostics()
    predef = {"CB_NO_STDRUNTIME": "1"} if nostd else None
    ld = Loader(d, include, nostd=nostd, predefined=predef)
    try:
        ld.load_entry(path)
    except CompileError as e:
        print(f"fatal: {e}")
        return
    n_loader = len(d.items)
    mods = ld.order
    try:
        ck = Checker(mods, d, C_RESERVED, nostd=nostd).run()
    except CompileError as e:
        print(f"fatal: {e}")
    for m in mods:
        if "/std/" in m.path.replace("\\", "/"):
            continue
        print(f"module {m.name}")
        for it in m.ast.items:
            print("item " + dump(it))
    for i, e in enumerate(d.items):
        if "/std/" in e.span.src.path.replace("\\", "/"):
            continue
        if i < n_loader:
            print(f"{e.level} {e.span.src.path}:{e.span.line}:{e.span.col} {e.msg}")
            continue
        notes = " ## ".join(e.notes)
        print(f"{e.level} {e.span.src.path}:{e.span.line}:{e.span.col}+{e.span.length} {e.msg} || {e.label or ''} || {e.help or ''} || {notes}")


main()
