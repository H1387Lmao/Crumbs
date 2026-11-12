#!/usr/bin/env python3
"""Crumbs launcher.

The main compiler is the self-hosted one (selfhost/*.cb).  It is built from the
committed C seed bootstrap/crumbsc.c with plain gcc (no Python involved in the
compiler itself) and cached in .crumbs-cache/.  This file only locates/builds it
and execs it.

The Python implementation in src/crumbsc is DEPRECATED legacy: it is kept for
`pyext` (Python extensions), for regenerating the seed, and as a parity oracle in
tests.  Use it explicitly with `--legacy` (first argument) or CRUMBS_LEGACY=1.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SEED = os.path.join(ROOT, "bootstrap", "crumbsc.c")
CACHE = os.path.join(ROOT, ".crumbs-cache")
BIN = os.path.join(CACHE, "crumbsc")


def legacy(argv):
    sys.path.insert(0, ROOT)
    from src.crumbsc.driver import main
    sys.argv = [sys.argv[0]] + argv
    return main()


# Features the self-hosted compiler does not implement yet; programs/flags using
# them are routed to the legacy Python compiler automatically.
LEGACY_FLAGS = ("--strict-casts", "-l", "-L", "--link", "-I", "--include", "--entry")
LEGACY_SRC = re.compile(r"^\s*(?:pub\s+)?async\s+fn\b|\bawait\b|^\s*link\s+\"", re.M)
IMPORT = re.compile(r"^\s*(?:pub\s+)?import\s+([A-Za-z_][\w.]*)", re.M)


def needs_legacy(argv):
    argv = argv[:argv.index("--")] if "--" in argv else argv
    if any(a in LEGACY_FLAGS or a.startswith(("-l", "-L", "-I", "--include=", "--link=", "--entry=")) for a in argv[1:]):
        return True
    if len(argv) < 2 or argv[1].startswith("-"):
        return True  # file not in the position the self-hosted CLI expects
    seen, todo = set(), [argv[1]]
    while todo:
        f = todo.pop()
        if f in seen or not os.path.isfile(f):
            continue
        seen.add(f)
        try:
            src = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if LEGACY_SRC.search(src):
            return True
        for m in IMPORT.finditer(src):
            todo.append(os.path.join(os.path.dirname(f), m.group(1).replace(".", os.sep) + ".cb"))
    return False


def ensure_compiler():
    if os.path.exists(BIN) and os.path.getmtime(BIN) >= os.path.getmtime(SEED):
        return BIN
    os.makedirs(CACHE, exist_ok=True)
    cc = os.environ.get("CC", "gcc")
    tmp = BIN + ".tmp%d" % os.getpid()
    r = subprocess.run([cc, "-std=gnu11", "-Uunix", "-O1", SEED, "-o", tmp, "-lm", "-pthread"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit("error: could not build the self-hosted compiler from bootstrap/crumbsc.c")
    os.replace(tmp, BIN)
    return BIN


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--legacy":
        return legacy(argv[1:])
    if os.environ.get("CRUMBS_LEGACY") or (argv and argv[0] not in ("check", "emit", "build", "run")):
        return legacy(argv)
    if needs_legacy(argv):
        return legacy(argv)
    exe = ensure_compiler()
    extra = ["--std=" + os.path.join(ROOT, "src", "std"),
             "--runtime=" + os.path.join(ROOT, "src", "crumbsc", "runtime.c")]
    if "--" in argv:
        k = argv.index("--")
        argv = argv[:k] + extra + argv[k:]
    else:
        argv = argv + extra
    sys.stdout.flush()
    os.execv(exe, [exe] + argv)


if __name__ == "__main__":
    sys.exit(main())
