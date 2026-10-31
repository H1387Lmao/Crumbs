"""Compilation pipeline and command-line interface."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

from . import __version__
from .checker import Checker
from .codegen import CodeGen, C_RESERVED
from .diag import Diagnostics, CompileError
from .loader import Loader


class LinkPlan:
    """What the C compiler needs besides the generated file: objects, libraries, pkg-config packages."""

    def __init__(self):
        self.files, self.libs, self.pkgs, self.incdirs, self.libdirs = [], [], [], [], []

    def add(self, lst, x):
        if x not in lst:
            lst.append(x)


def make_plan(mods):
    plan = LinkPlan()
    for m in mods:
        for kind, val, _ in m.directives:
            if kind == "file":
                plan.add(plan.files, val)
            elif kind == "lib":
                plan.add(plan.libs, val)
            elif kind == "pkg":
                plan.add(plan.pkgs, val)
            elif kind == "incdir":
                plan.add(plan.incdirs, val)
    return plan


def compile_to_c(path, include_dirs=(), color=None, quiet_warnings=False, strict=False, entry="main", gc=False):
    """Returns (c_source or None, diagnostics, link plan)."""
    d = Diagnostics()
    try:
        mods = Loader(d, include_dirs).load_entry(path)
    except CompileError as e:
        print(f"error: {e}", file=sys.stderr)
        return None, d, LinkPlan()
    plan = make_plan(mods)
    if d.has_errors:
        return None, d, plan
    externs = {it.name for m in mods if m.is_std for it in m.ast.items if it.kind == "Fn" and it.extern}
    ck = Checker(mods, d, C_RESERVED | externs, strict=strict, entry_name=entry).run()
    if d.has_errors:
        return None, d, plan
    cg = CodeGen(ck, gc=gc)
    return cg.generate(mods[-1].src.path), d, plan


def report(d, quiet_warnings=False):
    if quiet_warnings:
        d.items = [x for x in d.items if x.level == "error"]
    text = d.render()
    if text:
        print(text, file=sys.stderr)
    n, w = d.error_count, len(d.items) - d.error_count
    if n:
        print(f"error: aborting due to {n} error{'s' if n != 1 else ''}" + (f" ({w} warning{'s' if w != 1 else ''} emitted)" if w else ""),
              file=sys.stderr)
    elif w:
        print(f"{w} warning{'s' if w != 1 else ''} emitted", file=sys.stderr)


def find_cc(pref=None):
    for c in ([pref] if pref else []) + [os.environ.get("CC"), "gcc", "clang", "cc"]:
        if c and shutil.which(c):
            return c
    return None


SHARED_EXTS = (".so", ".dylib", ".dll")


def pkg_config(pkgs):
    """Flags from `pkg-config --cflags --libs`; raises CompileError-like RuntimeError on failure."""
    if not pkgs:
        return []
    exe = shutil.which("pkg-config") or shutil.which("pkgconf")
    if not exe:
        raise RuntimeError("`link pkg` needs pkg-config, which was not found on this machine")
    r = subprocess.run([exe, "--cflags", "--libs", *pkgs], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"pkg-config could not find {', '.join(pkgs)}:\n{r.stderr.strip()}")
    return r.stdout.split()


def cc_command(cc, c_path, out_path, opt, unchecked, extra, plan=None, libs=(), libdirs=(), files=(), pkgs=(), compile_only=False):
    plan = plan or LinkPlan()
    cmd = [cc, "-std=gnu11", f"-O{opt}", "-Wno-unused-function", "-Wno-unused-variable",
           "-Wno-unused-but-set-variable", "-Wno-unused-value"]
    if unchecked:
        cmd[1:1] = ["-DCB_UNCHECKED", "-fwrapv"]
    cmd += [f"-I{d}" for d in plan.incdirs]
    if compile_only:                       # no entry point: produce an object file for linking elsewhere
        return cmd + ["-c", c_path, "-o", out_path] + list(extra)
    cmd += [c_path]
    all_files = list(plan.files) + [os.path.abspath(f) for f in files]
    cmd += all_files
    rpaths = []
    for f in all_files:
        if f.lower().endswith(SHARED_EXTS):
            d = os.path.dirname(os.path.abspath(f))
            if d not in rpaths:
                rpaths.append(d)
    cmd += [f"-L{d}" for d in libdirs]
    cmd += [f"-Wl,-rpath,{d}" for d in rpaths]
    cmd += [f"-l{n}" for n in list(plan.libs) + list(libs)]
    cmd += pkg_config(list(plan.pkgs) + list(pkgs))
    cmd += ["-o", out_path, "-lm"]
    return cmd + list(extra)


def build(args, run_after=False):
    if run_after and not args.entry:
        print("error: `run` needs an entry point (this build has --entry=)", file=sys.stderr)
        return 1
    c_src, d, plan = compile_to_c(args.file, args.include, strict=args.strict_casts, entry=args.entry, gc=args.gc)
    report(d, args.no_warn)
    if c_src is None:
        return 1
    cc = find_cc(args.cc)
    if not cc:
        print("error: no C compiler found (looked for gcc, clang, cc); set --cc or $CC", file=sys.stderr)
        return 1
    stem = os.path.splitext(os.path.basename(args.file))[0]
    tmpdir = tempfile.mkdtemp(prefix="crumbs-")
    try:
        c_path = args.keep_c or os.path.join(tmpdir, stem + ".c")
        with open(c_path, "w") as f:
            f.write(c_src)
        no_entry = not args.entry
        out = args.output or (os.path.join(tmpdir, stem) if run_after else stem + (".o" if no_entry else ""))
        try:
            cmd = cc_command(cc, c_path, out, args.opt, args.unchecked, args.cflag or [], plan, compile_only=no_entry,
                             libs=args.lib or [], libdirs=args.libdir or [], files=args.link or [], pkgs=args.pkg or [])
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            linking = any(w in r.stderr for w in ("undefined reference", "ld returned", "cannot find -l", "Undefined symbols",
                                                  "cannot open shared object", "file format not recognized"))
            if linking:
                print("error: linking failed", file=sys.stderr)
                print(r.stderr, file=sys.stderr)
                print("help: check your `link` directives (or -l / -L / --link flags) and that the library matches this machine's architecture",
                      file=sys.stderr)
                return 1
            print("internal error: the C compiler rejected the generated code (this is a Crumbs bug):", file=sys.stderr)
            print(r.stderr, file=sys.stderr)
            print(f"generated C kept at {c_path}", file=sys.stderr)
            return 2
        if not run_after:
            return 0
        try:
            res = subprocess.run([out] + (args.args or []))
            return res.returncode
        except KeyboardInterrupt:
            return 130
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="crumbs", description="Crumbs: a small, type-safe, C-style language that compiles to C.")
    ap.add_argument("--version", action="version", version=f"crumbs {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("file", help="entry source file (.cb)")
        p.add_argument("-I", dest="include", action="append", default=[], help="extra module search directory")
        p.add_argument("--no-warn", action="store_true", help="hide warnings")
        p.add_argument("--strict-casts", action="store_true",
                       help="treat implicit-conversion and signedness warnings as errors")
        p.add_argument("--entry", default="main", metavar="NAME",
                       help="entry function (default main); `--entry=` builds an object file with no entry point")
        p.add_argument("--gc", action="store_true",
                       help="use the conservative garbage collector for alloc()/String (free() stays optional)")

    def cc_opts(p):
        p.add_argument("-O", dest="opt", default="2", choices=["0", "1", "2", "3", "s"], help="C optimization level (default 2)")
        p.add_argument("--unchecked", action="store_true",
                       help="disable overflow/bounds/div-by-zero checks for maximum speed")
        p.add_argument("--cc", help="C compiler to use (default: $CC, gcc, clang)")
        p.add_argument("--cflag", action="append", help="extra flag passed to the C compiler")
        p.add_argument("--keep-c", metavar="PATH", help="also write the generated C to PATH")
        p.add_argument("-l", dest="lib", action="append", metavar="NAME", help="link library NAME (-lNAME)")
        p.add_argument("-L", dest="libdir", action="append", metavar="DIR", help="add a library search directory")
        p.add_argument("--link", action="append", metavar="FILE", help="link an object file, archive, shared library or .c file")
        p.add_argument("--pkg", action="append", metavar="NAME", help="add flags from pkg-config NAME")

    p = sub.add_parser("build", help="compile to a native executable")
    common(p)
    cc_opts(p)
    p.add_argument("-o", "--output")
    p = sub.add_parser("run", help="compile and run")
    common(p)
    cc_opts(p)
    p.add_argument("-o", "--output")
    p.add_argument("args", nargs="*", help="arguments passed to the program (after --)")
    p = sub.add_parser("emit", help="print or write the generated C")
    common(p)
    p.add_argument("-o", "--output")
    p = sub.add_parser("check", help="type-check only")
    common(p)

    a = ap.parse_args(argv)
    try:
        return _dispatch(a)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # a compiler bug, not a user error
        import traceback
        traceback.print_exc()
        print(f"internal compiler error: {e!r}\nplease report this with the program that triggered it", file=sys.stderr)
        return 70


def _dispatch(a):
    if a.cmd == "check":
        c, d, _ = compile_to_c(a.file, a.include, strict=a.strict_casts, entry=a.entry, gc=a.gc)
        report(d, a.no_warn)
        return 0 if c is not None else 1
    if a.cmd == "emit":
        c, d, _ = compile_to_c(a.file, a.include, strict=a.strict_casts, entry=a.entry, gc=a.gc)
        report(d, a.no_warn)
        if c is None:
            return 1
        if a.output:
            with open(a.output, "w") as f:
                f.write(c)
        else:
            sys.stdout.write(c)
        return 0
    return build(a, run_after=(a.cmd == "run"))
