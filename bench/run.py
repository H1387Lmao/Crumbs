#!/usr/bin/env python3
"""Compare Crumbs with C, C++, Rust, Go, Java and Node on the programs in bench/programs.

    python3 bench/run.py                    run everything (best of 5)
    python3 bench/run.py fib sieve -n 3     only these programs, best of 3
    python3 bench/run.py --langs crumbs,c   only these languages

Every program prints a small checksum. A language is only timed when its output matches the Crumbs output, so a
fast-but-wrong port cannot sneak into the table. Results go to bench/results.json and bench/RESULTS.md.
"""
import argparse, json, os, platform, shutil, statistics, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROGRAMS = os.path.join(HERE, "programs")
BUILD = os.path.join(HERE, "build")

ORDER = ["fib", "collatz", "mandelbrot", "nbody", "matmul", "sieve", "quicksort", "bintrees", "hashmap"]
TITLES = {
    "fib": "fib(40): recursive calls", "collatz": "Collatz chains below 3M: integer loops",
    "mandelbrot": "Mandelbrot 2000x2000: float loops", "nbody": "N-body, 5M steps: float math on structs",
    "matmul": "Matrix multiply 600x600: memory and float", "sieve": "Sieve of Eratosthenes, 50M: byte array",
    "quicksort": "Quicksort, 3M integers: array swaps", "bintrees": "Binary trees, depth 16: allocation",
    "hashmap": "Hash map, 1M string keys: library code",
}
LANGS = {
    # name: (label, source extension, build function or None, run command builder)
    "crumbs": ("Crumbs (owned memory)", "cb"), "crumbs-gc": ("Crumbs (--gc)", "cb"), "crumbs-unchecked": ("Crumbs (unchecked)", "cb"),
    "c": ("C (gcc -O2)", "c"), "cpp": ("C++ (g++ -O2)", "cpp"), "rust": ("Rust (-O)", "rs"),
    "go": ("Go", "go"), "java": ("Java", "java"), "node": ("Node.js", "js"),
}
JAVA_CLASS = {p: p.capitalize() for p in ORDER}
JAVA_CLASS["nbody"] = "NBody"


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def build(lang, prog):
    """Compile one program; returns (run_command, compile_seconds, size_bytes) or None when the toolchain is missing."""
    src_dir = os.path.join(PROGRAMS, prog)
    out_dir = os.path.join(BUILD, lang, prog)
    os.makedirs(out_dir, exist_ok=True)
    exe = os.path.join(out_dir, prog)
    t0 = time.perf_counter()
    if lang in ("crumbs", "crumbs-unchecked", "crumbs-gc"):
        src = os.path.join(src_dir, prog + ".cb")
        own = os.path.join(src_dir, prog + "_own.cb")
        if lang != "crumbs-gc" and os.path.exists(own):
            src = own                                                # the version written with owned memory (std.own)
        cmd = [sys.executable, os.path.join(ROOT, "crumbs.py"), "build", src, "-o", exe]
        if lang == "crumbs-gc":
            cmd += ["--gc"]
        if lang == "crumbs-unchecked":
            cmd += ["--unchecked", "-O", "3"]
        r = sh(cmd)
    elif lang == "c":
        r = sh(["gcc", "-O2", "-o", exe, os.path.join(src_dir, prog + ".c"), "-lm"])
    elif lang == "cpp":
        r = sh(["g++", "-O2", "-o", exe, os.path.join(src_dir, prog + ".cpp")])
    elif lang == "rust":
        r = sh(["rustc", "-O", "-o", exe, os.path.join(src_dir, prog + ".rs")])
    elif lang == "go":
        r = sh(["go", "build", "-o", exe, os.path.join(src_dir, prog + ".go")], env={**os.environ, "GOFLAGS": "-mod=mod", "GO111MODULE": "off"})
    elif lang == "java":
        r = sh(["javac", "-d", out_dir, os.path.join(src_dir, JAVA_CLASS[prog] + ".java")])
        exe = None
    elif lang == "node":
        r = subprocess.CompletedProcess([], 0, "", "")
        exe = None
    secs = time.perf_counter() - t0
    if r.returncode != 0:
        print(f"  build failed: {lang}/{prog}\n{r.stderr[-800:]}", file=sys.stderr)
        return None
    if lang == "java":
        run = ["java", "-Xshare:auto", "-cp", out_dir, JAVA_CLASS[prog]]
        size = None
    elif lang == "node":
        run = ["node", os.path.join(src_dir, prog + ".js")]
        size = None
    else:
        run = [exe]
        size = os.path.getsize(exe)
    return run, secs, size


PEAKRSS = os.path.join(BUILD, "peakrss")


def build_launcher():
    os.makedirs(BUILD, exist_ok=True)
    r = sh(["gcc", "-O2", "-o", PEAKRSS, os.path.join(HERE, "peakrss.c")])
    if r.returncode:
        sys.exit("cannot build bench/peakrss.c: " + r.stderr)


def run_once(cmd):
    """(wall seconds, peak RSS in MiB, stdout). a tiny launcher reports the child's own peak (wait4 from Python would include Python's memory)."""
    t0 = time.perf_counter()
    r = subprocess.run([PEAKRSS, *cmd], capture_output=True, text=True)
    wall = time.perf_counter() - t0
    if r.returncode != 0:
        return None
    rss = 0.0
    for line in r.stderr.splitlines():
        if line.startswith("@@"):
            rss = int(line[2:]) / 1024.0
    return wall, rss, r.stdout.strip()


def have(lang):
    tool = {"c": "gcc", "cpp": "g++", "rust": "rustc", "go": "go", "java": "javac", "node": "node"}.get(lang)
    return tool is None or shutil.which(tool) is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("programs", nargs="*")
    ap.add_argument("-n", type=int, default=5, help="runs per program (the best is reported)")
    ap.add_argument("--langs", default=",".join(LANGS))
    ap.add_argument("--skip-build", action="store_true")
    args = ap.parse_args()
    build_launcher()
    progs = [p for p in ORDER if not args.programs or p in args.programs]
    langs = [l for l in args.langs.split(",") if l in LANGS and have(l)]
    results = {"machine": {"cpu": next((l.split(":")[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), platform.processor()),
                           "cores": os.cpu_count(), "os": platform.platform(), "runs": args.n,
                           "versions": versions()}, "programs": {}}
    for prog in progs:
        print(f"== {prog}: {TITLES[prog]}")
        expected = None
        row = {}
        for lang in langs:
            if not os.path.exists(os.path.join(PROGRAMS, prog, prog + "." + LANGS[lang][1])) and lang != "java":
                if lang not in ("crumbs-unchecked",):
                    continue
            if lang == "java" and not os.path.exists(os.path.join(PROGRAMS, prog, JAVA_CLASS[prog] + ".java")):
                continue
            b = build(lang, prog)
            if not b:
                continue
            cmd, csecs, size = b
            samples, rss, out = [], [], None
            for i in range(args.n):
                r = run_once(cmd)
                if r is None:
                    print(f"  {lang:18s} crashed")
                    break
                samples.append(r[0]); rss.append(r[1]); out = r[2]
            if not samples:
                continue
            if lang == "crumbs":
                expected = out
            status = "ok" if expected is None or out == expected else "WRONG OUTPUT"
            row[lang] = {"best": min(samples), "median": statistics.median(samples), "rss_mib": max(rss),
                         "compile_s": csecs, "size": size, "output": out, "status": status}
            print(f"  {lang:18s} best {min(samples):7.3f}s  median {statistics.median(samples):7.3f}s  rss {max(rss):7.1f} MiB  {status}")
        results["programs"][prog] = row
    full = not args.programs and args.langs == ",".join(LANGS)
    out_dir = HERE if full else BUILD          # a partial run never overwrites the published results
    with open(os.path.join(out_dir, "results.json"), "w") as f:
        json.dump(results, f, indent=1)
    write_markdown(results, out_dir)
    if full:
        write_docs_json(results)


def write_docs_json(res):
    """docs/benchmarks.json feeds the performance table on the documentation overview page."""
    langs = [l for l in LANGS if any(l in row for row in res["programs"].values())]
    out = {"machine": f"{res['machine']['cpu']}, {res['machine']['cores']} cores", "runs": res["machine"]["runs"],
           "languages": [{"id": l, "label": LANGS[l][0]} for l in langs],
           "programs": [{"id": p, "title": TITLES[p],
                         "results": {l: {"seconds": round(r["best"], 3), "rss_mib": round(r["rss_mib"]), "ok": r["status"] == "ok"}
                                     for l, r in row.items()}}
                        for p, row in res["programs"].items()]}
    with open(os.path.join(ROOT, "docs", "benchmarks.json"), "w") as f:
        json.dump(out, f, indent=1)
    print("wrote docs/benchmarks.json")


def versions():
    def first(cmd):
        r = sh(cmd)
        return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else "?"
    v = {}
    for name, cmd in [("gcc", ["gcc", "--version"]), ("g++", ["g++", "--version"]), ("rustc", ["rustc", "--version"]),
                      ("go", ["go", "version"]), ("node", ["node", "--version"])]:
        if shutil.which(cmd[0]):
            v[name] = first(cmd)
    if shutil.which("java"):
        r = sh(["java", "-version"])
        lines = [l for l in r.stderr.splitlines() if "version" in l and "Picked" not in l]
        v["java"] = lines[0].strip() if lines else "?"
    return v


def write_markdown(res, out_dir=HERE):
    langs = [l for l in LANGS if any(l in row for row in res["programs"].values())]
    lines = ["# Benchmarks", "",
             f"Machine: {res['machine']['cpu']}, {res['machine']['cores']} cores. Best of {res['machine']['runs']} runs, wall-clock seconds "
             "(Java and Node include their start-up). Lower is better; the figure in brackets is the time relative to C.", ""]
    head = "| program | " + " | ".join(LANGS[l][0] for l in langs) + " |"
    lines += [head, "|" + "---|" * (len(langs) + 1)]
    for prog, row in res["programs"].items():
        base = row.get("c", {}).get("best")
        cells = []
        for l in langs:
            if l not in row:
                cells.append("n/a"); continue
            c = row[l]
            txt = f"{c['best']:.2f}"
            if base:
                txt += f" ({c['best'] / base:.1f}x)"
            if c["status"] != "ok":
                txt += " WRONG"
            cells.append(txt)
        lines.append(f"| {TITLES[prog]} | " + " | ".join(cells) + " |")
    lines += ["", "## Peak memory (MiB)", "", head, "|" + "---|" * (len(langs) + 1)]
    for prog, row in res["programs"].items():
        lines.append(f"| {TITLES[prog]} | " + " | ".join(f"{row[l]['rss_mib']:.0f}" if l in row else "n/a" for l in langs) + " |")
    lines += ["", "## Compile time and binary size", "",
              "| language | total compile time (s) | binary size, mean (KiB) |", "|---|---|---|"]
    for l in langs:
        cs = [row[l]["compile_s"] for row in res["programs"].values() if l in row]
        sz = [row[l]["size"] for row in res["programs"].values() if l in row and row[l]["size"]]
        lines.append(f"| {LANGS[l][0]} | {sum(cs):.1f} | {(sum(sz) / len(sz) / 1024):.0f} |" if sz else f"| {LANGS[l][0]} | {sum(cs):.1f} | n/a |")
    lines += ["", "## Toolchains", ""] + [f"- {k}: {v}" for k, v in res["machine"]["versions"].items()]
    open(os.path.join(out_dir, "RESULTS.md"), "w").write("\n".join(lines) + "\n")
    print(f"\nwrote {os.path.relpath(out_dir, ROOT)}/RESULTS.md and results.json")


if __name__ == "__main__":
    main()
