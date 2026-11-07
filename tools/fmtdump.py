#!/usr/bin/env python3
"""For every line of a file: splits it as a format template and prints the pieces, each spec and its checks, in the
same format as selfhost/fmtdump.cb:  python3 tools/fmtdump.py FILE"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from crumbsc import fmtspec as F


def main():
    data = open(sys.argv[1], "rb").read()
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for ln, line in enumerate(lines, 1):
        print(f"T {ln}")
        try:
            pieces = F.parse_template(line)
        except F.SpecError as e:
            print(f"  error {e.msg} || {e.help}")
            continue
        for p in pieces:
            if p[0] == "lit":
                print(f"  lit {p[1].hex()}")
                continue
            _, ref, spec = p
            if ref is None:
                r = "none"
            elif isinstance(ref, int):
                r = f"int {ref}"
            elif isinstance(ref, tuple):
                r = f"expr {ref[1]}"
            else:
                r = f"name {ref}"
            print(f"  arg {r} spec={spec.encode().hex()}")
            try:
                sp = F.parse_spec(spec)
            except F.SpecError as e:
                print(f"    specerror {e.msg} || {e.help}")
                continue
            def b(x):
                return "true" if x else "false"
            print(f"    spec fill={sp.fill.encode().hex()} align={sp.align} sign={sp.sign.encode().hex()} alt={b(sp.alt)} "
                  f"zero={b(sp.zero)} width={sp.width} group={sp.group} prec={sp.prec} type={sp.type} plain={int(sp.plain)}")
            for k in ("int", "float", "bool", "text", "enum", "ptr", "other"):
                r = F.check_for(sp, k)
                print(f"    check {k} ok" if r is None else f"    check {k} {r[0]} || {r[1]}")


main()
