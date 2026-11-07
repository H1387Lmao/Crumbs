#!/usr/bin/env python3
"""Test runner.

  tests/ok/*.cb     must compile and run; stdout must equal the .out file next to it
  tests/err/*.cb    must fail to compile; lines marked `// ERROR: text` (or `// WARNING: text`)
                    must produce exactly those diagnostics on exactly those lines
  tests/panic/*.cb  must compile; first line `// PANIC: text` -> exit code 101 and stderr contains text
Files starting with `_` are helper modules and are skipped.
"""
import glob, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CRUMBS = [sys.executable, os.path.join(HERE, "..", "crumbs.py")]
DIAG = re.compile(r"^(error|warning): (.*)\n\s+--> ([^:\n]+):(\d+):\d+", re.M)
passed = failed = 0


def report(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok    {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}\n{detail}")


def run(args, **kw):
    return subprocess.run(CRUMBS + args, capture_output=True, text=True, **kw)


def test_ok(path, extra=(), exp_path=None):
    name = os.path.relpath(path, HERE)
    exp_path = exp_path or path[:-3] + ".out"
    in_path = path[:-3] + ".in"
    flags = re.match(r"//\s*FLAGS:\s*(.*)", open(path).readline())
    extra = list(extra) + (flags.group(1).split() if flags else [])
    r = run(["run", path, *extra], input=open(in_path).read() if os.path.exists(in_path) else None)
    expected = open(exp_path).read() if os.path.exists(exp_path) else None
    if r.returncode != 0:
        return report(name, False, f"exit {r.returncode}\n{r.stderr}")
    if expected is None:
        open(exp_path, "w").write(r.stdout)
        return report(name + " (recorded new .out)", True)
    if r.stdout != expected:
        import difflib
        d = "".join(difflib.unified_diff(expected.splitlines(1), r.stdout.splitlines(1), "expected", "actual"))
        return report(name, False, d)
    report(name, True)


def markers(path):
    out = []
    for i, line in enumerate(open(path).read().split("\n"), 1):
        m = re.search(r"//\s*(ERROR|WARNING):\s*(.*?)\s*$", line)
        if m:
            out.append((m.group(1).lower(), os.path.basename(path), i, m.group(2)))
    return out


def import_closure(path, seen=None):
    """The test file plus every `_helper.cb` module it (transitively) imports from its directory."""
    seen = seen if seen is not None else []
    if path in seen:
        return seen
    seen.append(path)
    for mod in re.findall(r"^\s*import\s+(_\w+)", open(path).read(), re.M):
        h = os.path.join(os.path.dirname(path), mod + ".cb")
        if os.path.exists(h):
            import_closure(h, seen)
    return seen


def test_err(path, extra=()):
    name = os.path.relpath(path, HERE)
    expect = []
    for f in import_closure(path):
        expect += markers(f)
    fl = re.match(r"//\s*FLAGS:\s*(.*)", open(path).readline())
    r = run(["check", path, *extra, *(fl.group(1).split() if fl else [])])
    got = [(k, os.path.basename(f), int(ln), msg) for k, msg, f, ln in DIAG.findall(r.stderr)]
    problems = []
    used = set()
    for kind, f, line, text in expect:
        hit = None
        for gi, (k, gf, ln, msg) in enumerate(got):
            if gi not in used and k == kind and gf == f and ln == line and text in msg:
                hit = gi
                break
        if hit is None:
            problems.append(f"missing {kind} at {f}:{line}: {text!r}")
        else:
            used.add(hit)
    for gi, (k, gf, ln, msg) in enumerate(got):
        if gi not in used and k == "error":
            problems.append(f"unexpected error at {gf}:{ln}: {msg}")
    if any(k == "error" for k, _, _, _ in expect) and r.returncode == 0:
        problems.append("compiler accepted an invalid program")
    if "Traceback" in r.stderr:
        problems.append("compiler crashed:\n" + r.stderr)
    report(name, not problems, "\n".join("        " + p for p in problems) + ("\n" + r.stderr if problems else ""))


def test_panic(path):
    name = os.path.relpath(path, HERE)
    head = open(path).read().split("\n")[:3]
    want = next(m.group(1) for m in (re.match(r"//\s*PANIC:\s*(.*?)\s*$", l) for l in head) if m)
    flags = next((m.group(1).split() for m in (re.match(r"//\s*FLAGS:\s*(.*)", l) for l in head) if m), [])
    r = run(["run", path, *flags])
    code = 1 if want.startswith("error:") else 101
    ok = r.returncode == code and want in r.stderr
    report(name, ok, f"exit={r.returncode} stderr={r.stderr!r} (wanted {want!r})")


def build_fixtures():
    """Prebuilt C artifacts for the interop tests (an object file and a shared library)."""
    d = os.path.join(HERE, "ok")
    out = os.path.join(d, "_build")
    os.makedirs(out, exist_ok=True)
    src = os.path.join(d, "_ffi_lib.c")
    cc = os.environ.get("CC", "gcc")
    for args in ([cc, "-c", "-fPIC", src, "-o", os.path.join(out, "_ffi_lib.o")],
                 [cc, "-shared", "-fPIC", src, "-o", os.path.join(out, "libffi_lib.so"), "-lm"]):
        subprocess.run(args, check=True)


def test_docs_fresh():
    """docs/stdlib.json is generated from src/std/*.cb: it must match what the generator produces now."""
    out = os.path.join(HERE, "..", "docs", "stdlib.json")
    before = open(out).read()
    r = subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "gen_stdlib_docs.py"), "--missing"],
                       capture_output=True, text=True)
    after = open(out).read()
    missing = [l for l in r.stdout.splitlines()[2:] if l.strip()]
    ok = r.returncode == 0 and before == after and not missing
    why = "run `python3 tools/gen_stdlib_docs.py` and commit docs/stdlib.json" if before != after else ""
    if missing:
        why += " undocumented: " + ", ".join(missing[:8])
    report("docs/stdlib.json is up to date", ok, why or r.stderr)


def test_selfhost(stage, tool_cb, py_tool, patterns, skip_async=False, extra=()):
    """The Crumbs-in-Crumbs `stage` (lexer, parser, ...) must print exactly what the Python one prints, for every file."""
    import tempfile, shutil
    tmp = tempfile.mkdtemp(prefix="selfhost-")
    exe = os.path.join(tmp, stage)
    try:
        r = run(["build", os.path.join(HERE, "..", "selfhost", tool_cb), "-o", exe, "--no-warn"])
        if r.returncode != 0:
            return report(f"selfhost/{stage}", False, r.stdout + r.stderr)
        files = sorted(f for pat in patterns for f in glob.glob(os.path.join(HERE, "..", pat)))
        if skip_async:   # the self-hosted parser does not desugar `async fn`
            files = [f for f in files if not any(w in open(f, encoding="utf-8", errors="replace").read() for w in ("async fn", "import std.async"))]
        bad = []
        for f in files:
            a = subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", py_tool), f, *extra], capture_output=True, text=True)
            b = subprocess.run([exe, f, *extra], capture_output=True, text=True)
            if a.stdout != b.stdout:
                bad.append(os.path.relpath(f, HERE))
        report(f"selfhost/{stage}: {len(files)} files identical to the Python {stage}", not bad, "differs on: " + ", ".join(bad[:10]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    only = sys.argv[1:]
    build_fixtures()
    def sel(p):
        return not os.path.basename(p).startswith("_") and (not only or any(o in p for o in only))
    print("== programs that must run")
    for p in sorted(glob.glob(os.path.join(HERE, "ok", "*.cb"))):
        if sel(p):
            test_ok(p)
    ex = os.path.join(HERE, "..", "examples", "ffi", "main.cb")
    if not only or "ffi" in " ".join(only):
        test_ok(ex, exp_path=os.path.join(HERE, "ok", "ffi_wrapper.out"))
    print("== programs that must be rejected")
    for p in sorted(glob.glob(os.path.join(HERE, "err", "*.cb"))):
        if sel(p):
            test_err(p)
    print("== programs checked with --strict-casts")
    for p in sorted(glob.glob(os.path.join(HERE, "strict", "*.cb"))):
        if sel(p):
            test_err(p, ["--strict-casts"])
    print("== programs that must panic")
    for p in sorted(glob.glob(os.path.join(HERE, "panic", "*.cb"))):
        if sel(p):
            test_panic(p)
    if not only or "selfhost" in " ".join(only):
        print("== the compiler written in Crumbs")
        pats = ["tests/ok/*.cb", "tests/err/*.cb", "tests/panic/*.cb", "tests/strict/*.cb", "src/std/*.cb", "selfhost/*.cb", "examples/*.cb", "examples/*/*.cb"]
        test_selfhost("lexer", "tokdump.cb", "tokdump.py", pats)
        test_selfhost("parser", "astdump.cb", "astdump.py", pats, skip_async=True)
        test_selfhost("loader", "loaddump.cb", "loaddump.py", pats + ["tests/selfhost_ld/*.cb"], skip_async=True,
                      extra=("--std=" + os.path.join(HERE, "..", "src", "std"),))
        test_selfhost("preprocessor", "ppdump.cb", "ppdump.py", pats + ["tests/selfhost_pp/*.cb"], extra=("-DMODE=2", "-DFEATURE"))
    if not only or "cookie" in " ".join(only):
        print("== cookie build tool")
        sys.path.insert(0, HERE)
        import test_cookie
        test_cookie.run_all(lambda name, ok, detail="": report(name, ok, detail))
    if not only or "docs" in " ".join(only):
        print("== documentation")
        test_docs_fresh()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
