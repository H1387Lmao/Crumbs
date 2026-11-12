#!/usr/bin/env python3
"""Runs the first phases of the checker on a program and prints the symbol tables, method tables, trait impls and
diagnostics, in the same format as selfhost/symdump.cb:  python3 tools/symdump.py FILE [--phase=decl|sigs]"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from crumbsc.diag import Diagnostics, CompileError
from crumbsc.loader import Loader
from crumbsc.checker import Checker
from crumbsc.codegen import C_RESERVED


def b(x):
    return "1" if x else "0"


def lst(xs):
    return "[" + ",".join(str(x) for x in xs) + "]"


def dump_sym(s):
    out = [f"  sym {s.kind} {s.name} pub={b(s.pub)} full={s.full} cname={s.cname} extern={b(s.extern)} export={b(s.export)}",
           f"variadic={b(s.variadic)} tparams={lst(s.tparams)} csym={s.csym} opaque={b(s.opaque)} xstruct={b(s.extern_struct)}",
           f"method={b(s.is_method)} has_self={b(s.has_self)} self_mut={b(s.self_mut)} drop={b(s.is_drop)} code={s.code}",
           f"variants={lst(s.variants)} node@{s.node.span.line}:{s.node.span.col}"]
    if s.kind == "trait":
        out.append(f"methods={lst(s.trait_methods)} supers={lst(x.full for x in s.supers)}")
    if s.drop_sym is not None:
        out.append(f"drop_sym={s.drop_sym.full}")
    bounds = getattr(s, "bounds", None)
    if bounds is not None:
        out.append("bounds={" + ",".join(f"{k}:{lst(t.full for t in v)}" for k, v in bounds.items()) + "}")
    return " ".join(out)


def main():
    path = sys.argv[1]
    phase = "decl"
    for a in sys.argv[2:]:
        if a.startswith("--phase="):
            phase = a[8:]
    d = Diagnostics()
    ld = Loader(d)
    try:
        ld.load_entry(path)
    except CompileError as e:
        print(f"fatal: {e}")
        return
    n_loader = len(d.items)
    mods = ld.order
    ck = Checker(mods, d, C_RESERVED)
    try:
        for m in mods:
            ck.declare(m)
            if m.name == "std.list" and m.is_std and "List" in m.syms:
                ck.prelude_types["List"] = m.syms["List"]
            if m.name == "std.iter" and m.is_std:
                for tn in ("Iter", "Pair", "Indexed"):
                    if tn in m.syms:
                        ck.prelude_types[tn] = m.syms[tn]
            if m.name == "std.fmt" and m.is_std and "Display" in m.syms:
                ck.prelude_traits["Display"] = m.syms["Display"]
        for m in mods:
            ck.resolve_bounds(m)
        ck.check_trait_impls()
        if phase != "decl":
            for m in mods:
                ck.resolve_sigs(m)
            ck.check_ffi()
            ck.check_struct_cycles()
    except CompileError as e:
        print(f"fatal: {e}")
    for m in mods:
        print(f"module {m.name}")
        for s in m.syms.values():
            print(dump_sym(s))
    for key, tab in ck.methods.items():
        print(f"methods {key}: " + ",".join(f"{n}={s.cname}" for n, s in tab.items()))
    for (tfull, key), info in ck.trait_impls.items():
        print(f"impl {tfull} {key} tparams={lst(info.tparams)} line={info.span.line} bounds={lst(info.bounds)}")
    print("fns " + lst(s.cname for s in ck.fns))
    print("structs " + lst(s.cname for s in ck.structs))
    print("enums " + lst(s.cname for s in ck.enums))
    print("consts " + lst(s.cname for s in ck.consts))
    print("traits " + lst(s.cname for s in ck.traits))
    print("errors " + lst(s.cname for s in ck.errors))
    if phase != "decl":
        for s in ck.structs:
            print(f"fields {s.cname}: " + ",".join(f"{n}:{t}" for n, t, _ in s.fields))
        for s in ck.fns:
            print(f"sig {s.cname}: ({','.join(str(p) for p in s.params)})->{s.ret}")
        for s in ck.consts:
            print(f"const {s.cname}: {s.ty}")
    for i, e in enumerate(d.items):
        if i < n_loader:     # lexer / parser / preprocessor diagnostics: message and position only
            print(f"{e.level} {e.span.src.path}:{e.span.line}:{e.span.col} {e.msg}")
            continue
        notes = " ## ".join(e.notes)
        print(f"{e.level} {e.span.src.path}:{e.span.line}:{e.span.col}+{e.span.length} {e.msg} || {e.label or ''} || {e.help or ''} || {notes}")


main()
