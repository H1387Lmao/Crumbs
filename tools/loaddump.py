#!/usr/bin/env python3
"""Loads a program and prints its modules (load order), imports, directives and diagnostics in the same format as
selfhost/loaddump.cb:  python3 tools/loaddump.py FILE [--nostd]"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from crumbsc.diag import Diagnostics, CompileError
from crumbsc.loader import Loader


def main():
    path = sys.argv[1]
    nostd = "--nostd" in sys.argv[2:]
    d = Diagnostics()
    ld = Loader(d, nostd=nostd)
    try:
        ld.load_entry(path)
    except CompileError as e:
        print(f"fatal: {e}")
    for m in ld.order:
        print(f"module {m.name} path={m.path} shown={os.path.relpath(m.path)} prefix={m.cprefix} "
              f"entry={int(m.is_entry)} std={int(m.is_std)}")
        for alias, im in m.imports.items():
            print(f"  import {alias} -> {im.mod.name} @{im.node.span.line}:{im.node.span.col}")
        for kind, val, sp in m.directives:
            print(f"  directive {kind} {val} @{sp.line}:{sp.col}")
    for e in d.items:
        print(f"{e.level} {e.span.src.path}:{e.span.line}:{e.span.col} {e.msg} || {e.notes[0] if e.notes else ''}")


main()
