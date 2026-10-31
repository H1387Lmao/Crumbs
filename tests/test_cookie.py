"""Tests for the cookie build tool: option forwarding, project.toml validation, and real builds."""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE = [sys.executable, os.path.join(HERE, "..", "cookie.py")]


def cookie(cwd, *args):
    return subprocess.run(COOKIE + ["--color", "never", *args], cwd=cwd, capture_output=True, text=True)


def run_all(report):
    tmp = tempfile.mkdtemp(prefix="cookie-test-")
    try:
        r = cookie(tmp, "new", "demo")
        report("cookie/new", r.returncode == 0 and os.path.isfile(os.path.join(tmp, "demo", "src", "main.cb")), r.stderr)
        proj = os.path.join(tmp, "demo")

        r = cookie(proj, "run")
        report("cookie/run", r.returncode == 0 and "Hello from demo!" in r.stdout, r.stdout + r.stderr)

        r = cookie(proj, "build", "--release")
        exe = os.path.join(proj, "build", "release", "demo")
        ok = r.returncode == 0 and os.path.isfile(exe)
        report("cookie/build --release", ok, r.stdout + r.stderr)
        if ok:
            out = subprocess.run([exe], capture_output=True, text=True).stdout
            report("cookie/built program runs", out == "Hello from demo!\n", out)

        # every option must reach the compiler under its real name
        r = cookie(proj, "build", "-n", "--no-gc", "--unchecked", "-O2", "--cflag=-Wall", "-l", "m", "-L", "/opt/x",
                   "--link", "a.o", "--pkg", "sdl2", "--entry=start", "--strict-casts", "--no-warn",
                   "--no-stdruntime", "-I", "lib", "-X=--keep-c=/tmp/x.c")
        cmd = next((l for l in r.stdout.splitlines() if "crumbs.py build" in l), "")
        want = ["--entry=start", "--no-gc", "-O2", "--unchecked", "--cflag=-Wall", "-l m", "-L /opt/x", "--link a.o",
                "--pkg sdl2", "--strict-casts", "--no-warn", "--no-stdruntime", "-I lib", "--keep-c=/tmp/x.c", "src/main.cb"]
        missing = [w for w in want if w not in cmd]
        report("cookie/forwards every option", not missing, f"missing {missing} in: {cmd}")

        r = cookie(proj, "build", "-n", "--release")
        cmd = next((l for l in r.stdout.splitlines() if "crumbs.py build" in l), "")
        report("cookie/profiles", "-O3" in cmd and "--overflow-panic" not in cmd, cmd)
        r = cookie(proj, "build", "-n")
        cmd = next((l for l in r.stdout.splitlines() if "crumbs.py build" in l), "")
        report("cookie/debug profile", "-O0" in cmd and "--overflow-panic" in cmd, cmd)
        r = cookie(proj, "build", "-n", "--no-overflow-panic", "-O1")
        cmd = next((l for l in r.stdout.splitlines() if "crumbs.py build" in l), "")
        report("cookie/cli overrides profile", "-O1" in cmd and "--overflow-panic" not in cmd, cmd)

        r = cookie(proj, "build", "--entry=")
        report("cookie/object file mode", r.returncode == 0 and os.path.isfile(os.path.join(proj, "build", "debug", "demo.o")),
                r.stdout + r.stderr)

        r = cookie(proj, "check")
        report("cookie/check", r.returncode == 0, r.stderr)
        r = cookie(proj, "emit")
        report("cookie/emit", r.returncode == 0 and "int main(void)" in r.stdout, r.stderr)

        # an error in the program makes the build fail with the compiler's diagnostics
        with open(os.path.join(proj, "src", "main.cb"), "w") as f:
            f.write("fn main() { let x: i32 = true; }\n")
        r = cookie(proj, "build")
        report("cookie/reports compile errors", r.returncode != 0 and "mismatched types" in r.stderr, r.stderr)

        # project.toml validation
        for text, want in (('[project]\nname = "x"\nentri = "m.cb"\n', "unknown key `entri`"),
                           ('[project]\nname = "x"\n[profile.fast]\nopt = 1\n', "unknown profile"),
                           ('[project]\nname = "x"\n[build]\ngc = "yes"\n', "must be true/false"),
                           ('[project]\nversion = "1"\n', "needs a `name`"),
                           ('[project]\nname = "x"\n[profile.debug]\nopt = 9\n', "must be one of")):
            with open(os.path.join(proj, "project.toml"), "w") as f:
                f.write(text)
            r = cookie(proj, "check")
            report(f"cookie/config: {want}", r.returncode != 0 and want in r.stderr, r.stderr)

        r = cookie(tmp, "check")
        report("cookie/no project", r.returncode != 0 and "project.toml" in r.stderr, r.stderr)

        with open(os.path.join(proj, "project.toml"), "w") as f:
            f.write('[project]\nname = "demo"\n')
        r = cookie(proj, "clean")
        report("cookie/clean", r.returncode == 0 and not os.path.exists(os.path.join(proj, "build")), r.stderr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
