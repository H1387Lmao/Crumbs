#!/usr/bin/env python3
"""Preprocesses a file and prints the result and the diagnostics in the same format as selfhost/ppdump.cb:
    python3 tools/ppdump.py FILE [-DNAME[=VALUE]] [-UNAME]"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from crumbsc.diag import Source, Diagnostics
from crumbsc.preprocessor import Preprocessor


def main():
    path = sys.argv[1]
    defines = {}
    for a in sys.argv[2:]:
        if a.startswith("-D"):
            k, _, v = a[2:].partition("=")
            defines[k] = v if "=" in a else "1"
        elif a.startswith("-U"):
            defines[a[2:]] = None
    text = open(path, encoding="utf-8").read()
    d = Diagnostics()
    pp = Preprocessor(d, defines)
    out = pp.process(text, path, Source(path, text))
    for e in d.items:
        print(f"{e.level} {e.span.line}:{e.span.col}+{e.span.length} {e.msg} || {e.label or ''} || {e.help or ''}")
    print("---")
    print(out)
    print("--- changed")
    for k in sorted(pp.changed):
        print(f"{k}\t{pp.changed[k]}")


main()
