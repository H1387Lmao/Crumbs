#!/usr/bin/env python3
"""Prints the tokens of a file in the same format as selfhost/tokdump.cb, so the two lexers can be compared:
    python3 tools/tokdump.py FILE"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from crumbsc.diag import Source, Diagnostics
from crumbsc.lexer import lex


def main():
    path = sys.argv[1]
    src = Source(path, open(path, encoding="utf-8").read())
    d = Diagnostics()
    toks = lex(src, d)
    for e in d.items:
        if e.level == "error":
            print(f"error {e.span.line}:{e.span.col} {e.msg}")
    for t in toks:
        pos = f"{t.span.line}:{t.span.col}+{t.span.length}"
        if t.kind == "int":
            print(f"{pos} int {t.val} {t.suffix or ''}")
        elif t.kind == "char":
            print(f"{pos} char {t.val}")
        elif t.kind == "float":
            print(f"{pos} float {t.suffix or ''}")
        elif t.kind == "str":
            print(f"{pos} str {t.val.hex()}")
        elif t.kind == "eof":
            print(f"{pos} eof ")
        else:
            print(f"{pos} {t.kind} {t.val}")


main()
