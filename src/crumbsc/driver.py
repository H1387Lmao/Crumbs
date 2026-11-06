"""Compilation pipeline and command-line interface."""
import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from . import __version__
from .checker import Checker
from .codegen import CodeGen, C_RESERVED
from .diag import Diagnostics, CompileError
from .loader import Loader
from .timing import TIMINGS


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


GC_MODES = ("auto", "thread", "manual")


def compile_to_c(path, include_dirs=(), color=None, quiet_warnings=False, strict=False, entry="main", gc=True,
                 nostd=False, overflow_panic=False, defines=None, gc_mode="auto", gc_interval=100, pyext=None, hold=None):
    """Returns (c_source or None, diagnostics, link plan)."""
    d = Diagnostics()
    predefined = {}
    if gc:
        predefined["CB_GC"] = "1"
        if gc_mode == "manual":
            predefined["CB_GC_MANUAL"] = "1"
        elif gc_mode == "thread":
            predefined["CB_GC_THREAD"] = "1"
    if nostd:
        predefined["CB_NO_STDRUNTIME"] = "1"
    if overflow_panic:
        predefined["CB_OVERFLOW_PANIC"] = "1"
    try:
        mods = Loader(d, include_dirs, nostd=nostd, defines=defines, predefined=predefined).load_entry(path)
    except CompileError as e:
        print(f"error: {e}", file=sys.stderr)
        return None, d, LinkPlan()
    TIMINGS.stats_modules = [m.name for m in mods]
    plan = make_plan(mods)
    if d.has_errors:
        return None, d, plan
    ck = Checker(mods, d, C_RESERVED, strict=strict, entry_name=entry, nostd=nostd, gc_mode=gc_mode if gc else "off").run()
    if d.has_errors:
        return None, d, plan
    cg = CodeGen(ck, gc=gc, nostd=nostd, overflow_panic=overflow_panic, gc_mode=gc_mode, gc_interval=gc_interval, pyext=pyext)
    if hold is not None:
        hold["cg"] = cg
    return cg.generate(mods[-1].src.path), d, plan


def ms(sec):
    return f"{sec * 1000:8.1f} ms" if sec < 10 else f"{sec:8.2f} s "


def verbose_header(args):
    if not args.verbose:
        return
    TIMINGS.enabled = True
    opts = [f"entry={args.entry or '(none)'}", "no-stdruntime" if args.no_stdruntime else ("gc" if args.gc else "no-gc"),
            "overflow-panic" if args.overflow_panic else "wrap-on-overflow"]
    if getattr(args, "unchecked", False):
        opts.append("unchecked")
    if args.strict_casts:
        opts.append("strict-casts")
    print(f"[crumbs] {__version__}  {args.file}", file=sys.stderr)
    print(f"[crumbs] options   {', '.join(opts)}", file=sys.stderr)


def verbose_front(args):
    """Timing lines for the compiler's own phases (everything before the C compiler runs)."""
    if not args.verbose:
        return
    T = TIMINGS
    mods = getattr(T, "stats_modules", [])
    out = sys.stderr

    def line(name, sec, extra=""):
        print(f"[crumbs] {name:<9} {ms(sec)}{('   ' + extra) if extra else ''}", file=out)
    if mods:
        shown = ", ".join(mods[:6]) + (f", ... (+{len(mods) - 6})" if len(mods) > 6 else "")
        print(f"[crumbs] modules   {len(mods)}: {shown}", file=out)
    if args.verbose > 1:
        for path, lines, toks, lx, ps in T.files:
            print(f"[crumbs]   {os.path.relpath(path) if os.path.isabs(path) else path:<38} {lines:>5} lines {toks:>6} tokens"
                  f"  lex {lx * 1000:6.2f} ms  parse {ps * 1000:6.2f} ms", file=out)
    n = T.calls.get("lex", 0)
    line("lex", T.excl.get("lex", 0), f"{n} file{'s' if n != 1 else ''}, {T.stats.get('tokens', 0):,} tokens")
    line("parse", T.excl.get("parse", 0), f"{T.stats.get('lines', 0):,} lines")
    line("load", T.excl.get("load", 0), "reading files, resolving imports")
    line("check", T.excl.get("check", 0), "types, moves, mutability")
    if "codegen" in T.excl:
        line("codegen", T.excl["codegen"], f"{T.stats.get('c_lines', 0):,} lines / {T.stats.get('c_bytes', 0) / 1024:.1f} KB of C")


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
    cmd = [cc, "-std=gnu11", "-Uunix", "-Ulinux", "-Ui386", f"-O{opt}", "-Wno-unused-function", "-Wno-unused-variable",
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
    cmd += ["-o", out_path, "-lm", "-pthread"]
    return cmd + list(extra)


def build(args, run_after=False):
    if run_after and not args.entry:
        print("error: `run` needs an entry point (this build has --entry=)", file=sys.stderr)
        return 1
    verbose_header(args)
    c_src, d, plan = compile_to_c(args.file, args.include, strict=args.strict_casts, entry=args.entry, gc=args.gc,
                                 nostd=args.no_stdruntime, overflow_panic=args.overflow_panic, defines=parse_defines(args),
                                 gc_mode=args.gc_mode, gc_interval=args.gc_interval)
    verbose_front(args)
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
        if args.verbose:
            print(f"[crumbs] cc        {' '.join(shlex.quote(c) for c in cmd)}", file=sys.stderr)
        t_cc = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, text=True)
        if args.verbose:
            print(f"[crumbs] cc        {ms(time.perf_counter() - t_cc)}   {cc} {'ok' if r.returncode == 0 else 'FAILED'}", file=sys.stderr)
            if r.returncode == 0:
                size = os.path.getsize(out) if os.path.exists(out) else 0
                print(f"[crumbs] output    {out} ({size / 1024:.1f} KB)", file=sys.stderr)
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
        if args.verbose:
            print(f"[crumbs] total     {ms(TIMINGS.total())}   (before running the program)" if run_after
                  else f"[crumbs] total     {ms(TIMINGS.total())}", file=sys.stderr)
        if not run_after:
            return 0
        try:
            res = subprocess.run([out] + (args.args or []))
            return res.returncode
        except KeyboardInterrupt:
            return 130
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def python_info(python):
    """(include dir, extension suffix) of a Python interpreter."""
    exe = shutil.which(python) or python
    code = "import sysconfig;print(sysconfig.get_paths()['include']);print(sysconfig.get_config_var('EXT_SUFFIX'))"
    try:
        r = subprocess.run([exe, "-c", code], capture_output=True, text=True)
    except OSError as e:
        raise RuntimeError(f"cannot run `{python}`: {e}")
    if r.returncode != 0 or len(r.stdout.split()) < 2:
        raise RuntimeError(f"`{python}` did not report its include directory: {r.stderr.strip()}")
    inc, suffix = r.stdout.split()[:2]
    if not os.path.exists(os.path.join(inc, "Python.h")):
        raise RuntimeError(f"{inc}/Python.h is missing: install the development headers of that Python (python3-dev)")
    return inc, suffix


def build_pyext(args):
    """`crumbs pyext FILE`: compile to a Python extension module (`NAME.cpython-3XX-....so` plus a `NAME.pyi` stub)."""
    import re as _re
    stem = os.path.splitext(os.path.basename(args.file))[0]
    modname = args.module or _re.sub(r"\W", "_", stem)
    if not modname.isidentifier():
        print(f"error: `{modname}` is not a valid Python module name (use --module NAME)", file=sys.stderr)
        return 1
    try:
        inc, suffix = python_info(args.python)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    args.entry = ""
    verbose_header(args)
    hold = {}
    c_src, d, plan = compile_to_c(args.file, args.include, strict=args.strict_casts, entry="", gc=args.gc,
                                 nostd=False, overflow_panic=args.overflow_panic, defines=parse_defines(args),
                                 gc_mode=args.gc_mode, gc_interval=args.gc_interval, pyext=modname, hold=hold)
    verbose_front(args)
    report(d, args.no_warn)
    if c_src is None:
        return 1
    cg = hold["cg"]
    for name, why in cg.py_notes:
        if name:
            print(f"note: `{name}` is not exposed to Python: {why}", file=sys.stderr)
        else:
            print(f"error: {why}", file=sys.stderr)
    if any(not n for n, _ in cg.py_notes):
        return 1
    cc = find_cc(args.cc)
    if not cc:
        print("error: no C compiler found (looked for gcc, clang, cc); set --cc or $CC", file=sys.stderr)
        return 1
    out = args.output or (modname + suffix)
    tmpdir = tempfile.mkdtemp(prefix="crumbs-")
    try:
        c_path = args.keep_c or os.path.join(tmpdir, modname + ".c")
        with open(c_path, "w") as f:
            f.write(c_src)
        try:
            cmd = cc_command(cc, c_path, out, args.opt, args.unchecked, ["-shared", "-fPIC", f"-I{inc}", "-Wno-deprecated-declarations"] + (args.cflag or []),
                             plan, libs=args.lib or [], libdirs=args.libdir or [], files=args.link or [], pkgs=args.pkg or [])
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if args.verbose:
            print(f"[crumbs] cc        {' '.join(shlex.quote(c) for c in cmd)}", file=sys.stderr)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print("internal error: the C compiler rejected the generated code (this is a Crumbs bug, or a `link` problem):", file=sys.stderr)
            print(r.stderr, file=sys.stderr)
            print(f"generated C kept at {c_path}", file=sys.stderr)
            if not args.keep_c:
                keep = os.path.join(os.getcwd(), modname + ".failed.c")
                shutil.copy(c_path, keep)
                print(f"(copied to {keep})", file=sys.stderr)
            return 2
        if not args.no_stub:
            stub_path = os.path.join(os.path.dirname(os.path.abspath(out)), modname + ".pyi")
            with open(stub_path, "w") as f:
                f.write(cg.py_stub)
        print(f"built {out}  (import {modname})")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def parse_defines(a):
    out = {}
    for item in getattr(a, "define", []) or []:
        name, _, val = item.partition("=")
        out[name] = val if _ else "1"
    for name in getattr(a, "undef", []) or []:
        out[name] = None
    return out


def preprocess_only(a):
    """`crumbs pp FILE`: print the entry file after preprocessing (like `gcc -E`)."""
    from .preprocessor import Preprocessor
    with open(a.file, encoding="utf-8") as f:
        text = f.read()
    d = Diagnostics()
    from .diag import Source
    src = Source(a.file, text)
    predef = {}
    if a.gc:
        predef["CB_GC"] = "1"
        if a.gc_mode == "manual":
            predef["CB_GC_MANUAL"] = "1"
        elif a.gc_mode == "thread":
            predef["CB_GC_THREAD"] = "1"
    if a.no_stdruntime:
        predef["CB_NO_STDRUNTIME"] = "1"
    if a.overflow_panic:
        predef["CB_OVERFLOW_PANIC"] = "1"
    out = Preprocessor(d, parse_defines(a), predef).process(text, a.file, src)
    report(d, a.no_warn)
    if d.has_errors:
        return 1
    sys.stdout.write(out if out.endswith("\n") else out + "\n")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="crumbs", description="Crumbs: a small, type-safe, C-style language that compiles to C.")
    ap.add_argument("--version", action="version", version=f"crumbs {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("file", help="entry source file (.cb)")
        p.add_argument("-I", dest="include", action="append", default=[], help="extra module search directory")
        p.add_argument("-v", "--verbose", action="count", default=0,
                       help="log what the compiler does: time spent lexing, parsing, checking and generating code, "
                            "and the C compiler command (-vv adds a per-file table)")
        p.add_argument("--no-warn", action="store_true", help="hide warnings")
        p.add_argument("-D", dest="define", action="append", default=[], metavar="NAME[=VALUE]",
                       help="define a preprocessor macro (value defaults to 1); usable in #if / #ifdef and in code")
        p.add_argument("-U", dest="undef", action="append", default=[], metavar="NAME",
                       help="undefine a predefined or -D macro")
        p.add_argument("--strict-casts", action="store_true",
                       help="treat implicit-conversion and signedness warnings as errors")
        p.add_argument("--entry", default="main", metavar="NAME",
                       help="entry function (default main); `--entry=` builds an object file with no entry point")
        p.add_argument("--gc", action=argparse.BooleanOptionalAction, default=None,
                       help="conservative garbage collector for alloc()/String/List (default: off; owned memory is the default, see std.own). "
                            "Without it, String/List/Own free themselves at scope end and alloc() memory is freed by hand")
        p.add_argument("--gc-mode", choices=GC_MODES, default="auto",
                       help="when the collector runs: auto (default) = the allocator starts a collection when the nursery is full; "
                            "thread = a background collector thread collects every --gc-interval ms (for beginners: nothing to manage); "
                            "manual = never automatically, only gc_collect() / gc_collect_gen(g) (for advanced users)")
        p.add_argument("--gc-interval", type=int, default=100, metavar="MS",
                       help="--gc-mode=thread: how often the collector thread wakes up, in milliseconds (default 100)")
        p.add_argument("--no-stdruntime", action="store_true",
                       help="freestanding build: only the integer/bounds helpers, no libc (no print, String, alloc, std.* imports)")
        p.add_argument("--overflow-panic", action="store_true",
                       help="panic on integer overflow in + - * and negation (default: wrap around like C)")

    def cc_opts(p):
        p.add_argument("-O", dest="opt", default="2", choices=["0", "1", "2", "3", "s"], help="C optimization level (default 2)")
        p.add_argument("--unchecked", action="store_true",
                       help="remove bounds, null, division and conversion checks (and overflow panics) for maximum speed")
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
    p = sub.add_parser("pyext", help="compile to a Python extension module (an importable .so)")
    common(p)
    cc_opts(p)
    p.add_argument("-o", "--output", help="output file (default NAME.cpython-3XX-<platform>.so in the current directory)")
    p.add_argument("--module", metavar="NAME", help="the Python module name (default: the file's name); must match the .so file name")
    p.add_argument("--python", default="python3", metavar="EXE", help="the Python to build for (default python3)")
    p.add_argument("--no-stub", action="store_true", help="do not write the NAME.pyi type stub")
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
    p = sub.add_parser("pp", help="print the entry file after preprocessing (#define / #if / ...)")
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


def check_gc_flags(a):
    """Reject flag combinations that cannot work; returns an exit code or None."""
    if a.gc is None:                    # the collector is opt-in; asking for one of its modes turns it on
        a.gc = a.gc_mode != "auto"
    if not 1 <= a.gc_interval <= 60000:
        print("error: --gc-interval must be between 1 and 60000 milliseconds", file=sys.stderr)
        return 2
    if a.gc_mode != "auto" and not a.gc and not a.no_stdruntime:
        print(f"error: `--gc-mode={a.gc_mode}` needs the collector, but `--no-gc` is set", file=sys.stderr)
        print("help: drop --no-gc, or drop --gc-mode", file=sys.stderr)
        return 2
    return None


def _dispatch(a):
    a.file_ = a.file
    bad = check_gc_flags(a)
    if bad is not None:
        return bad
    if a.cmd == "pp":
        return preprocess_only(a)
    if a.cmd == "pyext":
        return build_pyext(a)
    if a.cmd in ("check", "emit"):
        verbose_header(a)
    if a.cmd == "check":
        c, d, _ = compile_to_c(a.file, a.include, strict=a.strict_casts, entry=a.entry, gc=a.gc,
                                 nostd=a.no_stdruntime, overflow_panic=a.overflow_panic, defines=parse_defines(a),
                                 gc_mode=a.gc_mode, gc_interval=a.gc_interval)
        verbose_front(a)
        report(d, a.no_warn)
        return 0 if c is not None else 1
    if a.cmd == "emit":
        c, d, _ = compile_to_c(a.file, a.include, strict=a.strict_casts, entry=a.entry, gc=a.gc,
                                 nostd=a.no_stdruntime, overflow_panic=a.overflow_panic, defines=parse_defines(a),
                                 gc_mode=a.gc_mode, gc_interval=a.gc_interval)
        verbose_front(a)
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
