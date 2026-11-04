#!/usr/bin/env python3
"""cookie: the build tool for Crumbs projects.

    cookie new my_game          create a project in ./my_game
    cookie init                 turn the current directory into a project
    cookie build [--release]    compile the project to a native executable
    cookie run   [--release] -- program arguments
    cookie check                type-check only
    cookie emit                 print the generated C
    cookie clean                delete the build directory
    cookie info                 show the project and the exact compiler commands

Every option below is forwarded to the Crumbs compiler under the same name, so
`cookie build --gc -O3` and `crumbs build --gc -O3 src/main.cb` mean the same thing.
Settings come from project.toml and can be overridden on the command line.
"""
import argparse
import difflib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

__version__ = "0.2.0"

HERE = Path(__file__).resolve().parent
MANIFEST = "project.toml"
OPT_LEVELS = ("0", "1", "2", "3", "s")
PROFILES = ("debug", "release")


# ===================================================================== output
class Style:
    enabled = None

    @classmethod
    def on(cls, stream):
        if cls.enabled is not None:
            return cls.enabled
        return stream.isatty() and "NO_COLOR" not in os.environ


def paint(text, code, stream=None):
    stream = stream or sys.stdout
    return f"\033[{code}m{text}\033[0m" if Style.on(stream) else str(text)


def status(verb, message):
    """Cargo-style progress line: a right-aligned bold verb followed by the message."""
    print(f"{paint(verb.rjust(12), '1;32')} {message}", flush=True)


def note(message):
    print(f"{paint('note:'.rjust(12), '1;36')} {message}", flush=True)


def warn(message, help=None):
    print(f"{paint('warning:', '1;33', sys.stderr)} {message}", file=sys.stderr)
    if help:
        print(f"{paint('help:', '1;36', sys.stderr)} {help}", file=sys.stderr)


def fail(message, help=None):
    print(f"{paint('error:', '1;31', sys.stderr)} {message}", file=sys.stderr)
    if help:
        print(f"{paint('help:', '1;36', sys.stderr)} {help}", file=sys.stderr)
    return 1


class CookieError(Exception):
    def __init__(self, message, help=None):
        super().__init__(message)
        self.help = help


def suggest(name, options):
    m = difflib.get_close_matches(name, list(options), n=1)
    return f"did you mean `{m[0]}`?" if m else None


# ===================================================================== config
def load_toml(path):
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        try:
            import tomli as tomllib
        except ImportError:
            raise CookieError("Python 3.11 or newer is needed to read project.toml",
                              "upgrade Python, or `pip install tomli`")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise CookieError(f"{path.name} is not valid TOML: {e}")


STR_LIST = "list of strings"
GC_MODES = ("auto", "thread", "manual")

PROJECT_KEYS = {"name": str, "version": str, "description": str, "source_dir": str, "entry": str,
                "entry_fn": str, "build_dir": str, "crumbs_path": str}
BUILD_KEYS = {"cc": str, "gc": bool, "gc_mode": str, "gc_interval": int, "no_stdruntime": bool, "strict_casts": bool, "no_warn": bool,
              "include": STR_LIST, "libs": STR_LIST, "lib_dirs": STR_LIST, "link": STR_LIST,
              "pkg": STR_LIST, "cflags": STR_LIST, "defines": STR_LIST}
PROFILE_KEYS = {"opt": (str, int), "unchecked": bool, "overflow_panic": bool, "gc": bool, "gc_mode": str, "gc_interval": int,
                "strict_casts": bool}


def check_table(table, schema, where):
    if not isinstance(table, dict):
        raise CookieError(f"`[{where}]` must be a table")
    for key, value in table.items():
        if key not in schema:
            raise CookieError(f"unknown key `{key}` in [{where}]", suggest(key, schema) or
                              f"known keys: {', '.join(sorted(schema))}")
        want = schema[key]
        if want == STR_LIST:
            ok = isinstance(value, list) and all(isinstance(x, str) for x in value)
            what = "a list of strings"
        else:
            types = want if isinstance(want, tuple) else (want,)
            ok = isinstance(value, types) and not (bool not in types and isinstance(value, bool))
            what = " or ".join({str: "a string", bool: "true/false", int: "an integer"}[t] for t in types)
        if not ok:
            raise CookieError(f"`{key}` in [{where}] must be {what}")
    if "gc_mode" in table and table["gc_mode"] not in GC_MODES:
        raise CookieError(f"`gc_mode` in [{where}] must be one of {', '.join(GC_MODES)}, not `{table['gc_mode']}`")
    if "gc_interval" in table and not 1 <= table["gc_interval"] <= 60000:
        raise CookieError(f"`gc_interval` in [{where}] must be between 1 and 60000 (milliseconds)")
    if "opt" in table and str(table["opt"]) not in OPT_LEVELS:
        raise CookieError(f"`opt` in [{where}] must be one of {', '.join(OPT_LEVELS)}, not `{table['opt']}`")


class Project:
    """A project.toml plus the directory it lives in."""

    def __init__(self, root, config):
        self.root = root
        self.config = config
        for key in config:
            if key not in ("project", "build", "profile"):
                raise CookieError(f"unknown section `{key}` in {MANIFEST}",
                                  suggest(key, ("project", "build", "profile")))
        self.p = config.get("project", {})
        check_table(self.p, PROJECT_KEYS, "project")
        self.b = config.get("build", {})
        check_table(self.b, BUILD_KEYS, "build")
        profiles = config.get("profile", {})
        if not isinstance(profiles, dict):
            raise CookieError("`[profile]` must be a table")
        for name, table in profiles.items():
            if name not in PROFILES:
                raise CookieError(f"unknown profile `{name}`", suggest(name, PROFILES) or "profiles: debug, release")
            check_table(table, PROFILE_KEYS, f"profile.{name}")
        self.profiles = profiles
        if "name" not in self.p:
            raise CookieError(f"[project] needs a `name` in {MANIFEST}", f'add `name = "{root.name}"`')

    @classmethod
    def find(cls, start, explicit=None):
        if explicit:
            path = Path(explicit).expanduser().resolve()
            if path.is_file():
                path = path.parent
            if not (path / MANIFEST).is_file():
                raise CookieError(f"no {MANIFEST} in {path}")
            return cls(path, load_toml(path / MANIFEST))
        here = start.resolve()
        for d in (here, *here.parents):
            if (d / MANIFEST).is_file():
                return cls(d, load_toml(d / MANIFEST))
        raise CookieError(f"could not find {MANIFEST} in {here} or any parent directory",
                          "create a project with `cookie init` or `cookie new NAME`")

    # -- plain settings
    @property
    def name(self):
        return self.p["name"]

    @property
    def version(self):
        return self.p.get("version", "0.1.0")

    @property
    def source_dir(self):
        return self.p.get("source_dir", "src")

    @property
    def entry_file(self):
        return self.root / self.source_dir / self.p.get("entry", "main.cb")

    @property
    def entry_fn(self):
        return self.p.get("entry_fn", "main")

    def build_dir(self, profile):
        return self.root / self.p.get("build_dir", "build") / profile

    def crumbs(self):
        """The compiler script: $CRUMBS, then `crumbs_path`, then the checkout this cookie belongs to."""
        raw = os.environ.get("CRUMBS") or self.p.get("crumbs_path")
        path = Path(raw).expanduser() if raw else HERE
        if raw and not path.is_absolute():
            path = self.root / path
        if path.is_dir():
            path = path / "crumbs.py"
        if not path.is_file():
            raise CookieError(f"Crumbs compiler not found: {path}",
                              "fix `crumbs_path` in project.toml or set the CRUMBS environment variable")
        return path.resolve()

    def profile_table(self, profile):
        base = DEFAULT_PROFILES[profile]
        return {**base, **self.profiles.get(profile, {})}


DEFAULT_PROFILES = {
    "debug": {"opt": "0", "overflow_panic": True},
    "release": {"opt": "3"},
}


# ============================================================= option resolving
class Options:
    """Everything that decides how the compiler is invoked, after merging
    command line > profile > [build] > defaults."""

    FIELDS = ("opt", "unchecked", "overflow_panic", "gc", "gc_mode", "gc_interval", "no_stdruntime", "strict_casts", "no_warn", "cc")

    def __init__(self, project, args, profile):
        self.profile = profile
        table = {**project.b, **project.profile_table(profile)}

        def pick(name, default=None):
            cli = getattr(args, name, None)
            if cli is not None:
                return cli
            return table.get(name, default)

        self.opt = str(pick("opt", "2"))
        self.unchecked = bool(pick("unchecked", False))
        self.overflow_panic = bool(pick("overflow_panic", False))
        gc_set = pick("gc", None)
        self.gc_mode = str(pick("gc_mode", "auto"))
        self.gc = bool(gc_set) if gc_set is not None else self.gc_mode != "auto"     # a collector mode implies the collector
        self.gc_interval = int(pick("gc_interval", 100))
        self.no_stdruntime = bool(pick("no_stdruntime", False))
        self.strict_casts = bool(pick("strict_casts", False))
        self.no_warn = bool(pick("no_warn", False))
        self.cc = pick("cc")
        entry = getattr(args, "entry", None)
        self.entry = project.entry_fn if entry is None else entry
        self.include = list(project.b.get("include", [])) + list(getattr(args, "include", None) or [])
        self.libs = list(project.b.get("libs", [])) + list(getattr(args, "lib", None) or [])
        self.lib_dirs = list(project.b.get("lib_dirs", [])) + list(getattr(args, "libdir", None) or [])
        self.link = list(project.b.get("link", [])) + list(getattr(args, "link", None) or [])
        self.pkg = list(project.b.get("pkg", [])) + list(getattr(args, "pkg", None) or [])
        self.cflags = list(project.b.get("cflags", [])) + list(getattr(args, "cflag", None) or [])
        self.defines = list(project.b.get("defines", [])) + list(getattr(args, "define", None) or [])
        self.undefs = list(getattr(args, "undef", None) or [])
        self.extra = list(getattr(args, "crumbs_arg", None) or [])
        self.verbose = int(getattr(args, "verbose", 0) or 0)
        if self.unchecked and getattr(args, "overflow_panic", None) is True:
            warn("`--overflow-panic` has no effect together with `--unchecked`",
                 "--unchecked removes every runtime check, including overflow panics")
        if self.gc_mode != "auto" and not self.gc and not self.no_stdruntime:
            raise CookieError(f"`gc_mode = \"{self.gc_mode}\"` needs the collector, but `gc` is off",
                              "set `gc = true`, or use the default `gc_mode = \"auto\"`")
        if self.no_stdruntime and self.gc and getattr(args, "gc", None) is True:
            warn("`--gc` has no effect together with `--no-stdruntime`")

    def summary(self):
        bits = [f"-O{self.opt}"]
        if self.unchecked:
            bits.append("unchecked")
        elif self.overflow_panic:
            bits.append("overflow-panic")
        if self.no_stdruntime:
            bits.append("no-stdruntime")
        else:
            bits.append(("gc" if self.gc_mode == "auto" else f"gc:{self.gc_mode}") if self.gc else "no-gc")
        if self.strict_casts:
            bits.append("strict-casts")
        return ", ".join(bits)

    def common_flags(self):
        flags = ["-v"] * self.verbose
        for d in self.include:
            flags += ["-I", d]
        if self.no_warn:
            flags.append("--no-warn")
        if self.strict_casts:
            flags.append("--strict-casts")
        flags.append(f"--entry={self.entry}")
        flags.append("--gc" if self.gc else "--no-gc")
        if self.gc_mode != "auto":
            flags.append(f"--gc-mode={self.gc_mode}")
        if self.gc_mode == "thread" and self.gc_interval != 100:
            flags.append(f"--gc-interval={self.gc_interval}")
        if self.no_stdruntime:
            flags.append("--no-stdruntime")
        if self.overflow_panic:
            flags.append("--overflow-panic")
        return flags

    def cc_flags(self):
        flags = [f"-O{self.opt}"]
        if self.unchecked:
            flags.append("--unchecked")
        if self.cc:
            flags += ["--cc", self.cc]
        for f in self.cflags:
            flags.append(f"--cflag={f}")
        for dflt in self.defines:
            flags += ["-D", dflt]
        for u in self.undefs:
            flags += ["-U", u]
        for n in self.libs:
            flags += ["-l", n]
        for d in self.lib_dirs:
            flags += ["-L", d]
        for f in self.link:
            flags += ["--link", f]
        for p in self.pkg:
            flags += ["--pkg", p]
        return flags


def compiler_command(project, sub, opts, output=None, keep_c=None, program_args=()):
    cmd = [sys.executable, str(project.crumbs()), sub, *opts.common_flags()]
    if sub in ("build", "run"):
        cmd += opts.cc_flags()
    if sub == "build" and output:
        cmd += ["-o", str(output)]
    if sub == "emit" and output:
        cmd += ["-o", str(output)]
    if keep_c and sub in ("build", "run"):
        cmd += ["--keep-c", str(keep_c)]
    cmd += opts.extra
    cmd.append(str(project.entry_file.relative_to(project.root)))
    if sub == "run" and program_args:
        cmd += ["--", *program_args]
    return cmd


# ================================================================== running
def show(cmd):
    return " ".join(shlex.quote(c) for c in cmd)


def execute(cmd, cwd, args, quiet_ok=False):
    if getattr(args, "verbose", False) or getattr(args, "dry_run", False):
        note(f"{show(cmd)}")
    if getattr(args, "dry_run", False):
        return 0
    try:
        return subprocess.run(cmd, cwd=cwd).returncode
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        raise CookieError(f"could not start the Crumbs compiler: {e.strerror}")


def prepare(args):
    project = Project.find(Path.cwd(), getattr(args, "directory", None))
    entry = project.entry_file
    if not entry.is_file():
        raise CookieError(f"entry file not found: {entry.relative_to(project.root)}",
                          f"create it, or set `source_dir` / `entry` in {MANIFEST}")
    profile = "release" if getattr(args, "release", False) else "debug"
    opts = Options(project, args, profile)
    return project, opts, profile


def label(project):
    return f"{project.name} v{project.version} ({project.root})"


def cmd_build(args):
    project, opts, profile = prepare(args)
    out_dir = project.build_dir(profile)
    if args.output:
        output = Path(args.output).expanduser().resolve()
    else:
        output = out_dir / (project.name + (".o" if not opts.entry else ""))
    keep_c = None
    if args.keep_c is not None:
        keep_c = out_dir / f"{project.name}.c" if args.keep_c is True else Path(args.keep_c).expanduser().resolve()
    if not args.dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
        if keep_c:
            keep_c.parent.mkdir(parents=True, exist_ok=True)
    status("Compiling", label(project))
    t0 = time.time()
    code = execute(compiler_command(project, "build", opts, output, keep_c), project.root, args)
    if code != 0:
        return fail(f"could not compile `{project.name}`")
    shown = os.path.relpath(output, Path.cwd()) if not args.dry_run else output
    status("Finished", f"{profile} [{opts.summary()}] -> {shown} in {time.time() - t0:.2f}s")
    return 0


def cmd_run(args):
    project, opts, profile = prepare(args)
    if not opts.entry:
        raise CookieError("this project has no entry point (entry_fn is empty), so there is nothing to run",
                          "build it instead with `cookie build`")
    keep_c = None
    if args.keep_c is not None:
        keep_c = project.build_dir(profile) / f"{project.name}.c" if args.keep_c is True else Path(args.keep_c).resolve()
        keep_c.parent.mkdir(parents=True, exist_ok=True)
    status("Running", f"{project.name} [{profile}: {opts.summary()}]")
    return execute(compiler_command(project, "run", opts, keep_c=keep_c, program_args=args.program_args),
                   project.root, args)


def cmd_check(args):
    project, opts, profile = prepare(args)
    status("Checking", label(project))
    code = execute(compiler_command(project, "check", opts), project.root, args)
    if code != 0:
        return fail(f"`{project.name}` has errors")
    status("Finished", "no errors")
    return 0


def cmd_emit(args):
    project, opts, profile = prepare(args)
    output = Path(args.output).expanduser().resolve() if args.output else None
    return execute(compiler_command(project, "emit", opts, output), project.root, args)


def cmd_clean(args):
    project = Project.find(Path.cwd(), args.directory)
    root = (project.root / project.p.get("build_dir", "build")).resolve()
    if root == project.root or project.root.is_relative_to(root) or not root.is_relative_to(project.root):
        raise CookieError(f"refusing to delete `{root}`: the build directory must be a folder inside the project",
                          "check `build_dir` in project.toml")
    if not root.exists():
        status("Clean", "nothing to do")
        return 0
    if args.dry_run:
        note(f"would delete {root}")
        return 0
    shutil.rmtree(root)
    status("Removed", str(root.relative_to(project.root)))
    return 0


def cmd_info(args):
    project = Project.find(Path.cwd(), args.directory)
    info = {
        "name": project.name, "version": project.version, "root": str(project.root),
        "entry": str(project.entry_file.relative_to(project.root)),
        "entry_fn": project.entry_fn, "compiler": str(project.crumbs()),
        "build_dir": str(project.build_dir("<profile>").parent.relative_to(project.root)),
        "commands": {},
    }
    for profile in PROFILES:
        opts = Options(project, argparse.Namespace(), profile)
        out = project.build_dir(profile) / (project.name + (".o" if not opts.entry else ""))
        info["commands"][profile] = show(compiler_command(project, "build", opts, out)[1:]) \
            .replace(str(project.crumbs()), "crumbs.py", 1)
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    print(f"{paint(info['name'], '1')} v{info['version']}")
    for key in ("root", "entry", "entry_fn", "compiler", "build_dir"):
        print(f"  {key:<10} {info[key] or '(none)'}")
    for profile in PROFILES:
        print(f"  {profile + ':':<10} {info['commands'][profile]}")
    return 0


# ===================================================================== init
GITIGNORE = "build/\n__pycache__/\n*.o\n*.c.keep\n"

MAIN_TEMPLATE = '''// {name}
fn main() {{
    println("Hello from {name}!");
}}
'''

LIB_TEMPLATE = '''// {name}: built as an object file with `cookie build` (entry_fn = "")
export fn {ident}_add(a: i32, b: i32) -> i32 {{
    return a + b;
}}
'''

MANIFEST_TEMPLATE = '''[project]
name = "{name}"
version = "0.1.0"
source_dir = "src"
entry = "{entry}"
{entry_fn}build_dir = "build"

# [build]
# gc = true                 # conservative garbage collector (default); false = plain malloc
# gc_mode = "auto"          # "auto": collect when needed | "thread": background collector thread | "manual": only gc_collect()/gc_collect_gen()
# gc_interval = 100         # gc_mode = "thread": milliseconds between collections
# no_stdruntime = false     # freestanding build without libc
# strict_casts = false
# include = ["lib"]         # extra module search directories (-I)
# libs = ["m"]              # libraries to link (-l)
# lib_dirs = []             # library search directories (-L)
# link = []                 # object files / archives / shared libraries to link
# pkg = []                  # pkg-config packages
# cflags = []               # extra C compiler flags
# defines = []              # preprocessor macros, like -D: ["FEATURE", "LEVEL=3"]

[profile.debug]
opt = "0"
overflow_panic = true

[profile.release]
opt = "3"
'''


def sanitize(name):
    out = "".join(c if (c.isalnum() or c in "_-") else "_" for c in name.strip())
    return out or "app"


def make_project(target, name, force, lib):
    if target.exists() and not target.is_dir():
        raise CookieError(f"`{target}` exists and is not a directory")
    manifest = target / MANIFEST
    if manifest.exists() and not force:
        raise CookieError(f"`{target}` already contains a {MANIFEST}", "use --force to overwrite it")
    if target.exists() and any(target.iterdir()) and not force and not manifest.exists():
        pass                                  # an existing, non-empty folder is fine as long as there is no project in it
    entry = "lib.cb" if lib else "main.cb"
    target.mkdir(parents=True, exist_ok=True)
    manifest.write_text(MANIFEST_TEMPLATE.format(name=name, entry=entry, entry_fn='entry_fn = ""\n' if lib else ""),
                        encoding="utf-8")
    src = target / "src"
    src.mkdir(exist_ok=True)
    entry_path = src / entry
    created = [MANIFEST]
    if not entry_path.exists():
        template = LIB_TEMPLATE if lib else MAIN_TEMPLATE
        entry_path.write_text(template.format(name=name, ident=name.replace("-", "_")), encoding="utf-8")
        created.append(f"src/{entry}")
    ignore = target / ".gitignore"
    if not ignore.exists():
        ignore.write_text(GITIGNORE, encoding="utf-8")
        created.append(".gitignore")
    return created


def cmd_init(args):
    target = Path(args.path).expanduser().resolve()
    name = sanitize(args.name or target.name)
    created = make_project(target, name, args.force, args.lib)
    status("Created", f"{'library' if args.lib else 'binary'} project `{name}` in {target}")
    note("wrote " + ", ".join(created))
    return 0


def cmd_new(args):
    target = Path(args.name).expanduser().resolve()
    if target.exists():
        raise CookieError(f"`{target}` already exists", "pick another name, or use `cookie init` inside it")
    name = sanitize(target.name)
    created = make_project(target, name, False, args.lib)
    status("Created", f"{'library' if args.lib else 'binary'} project `{name}` in {target}")
    note("wrote " + ", ".join(created))
    note(f"next: cd {args.name} && cookie run")
    return 0


# ================================================================== parser
def add_common(p):
    g = p.add_argument_group("project")
    g.add_argument("-C", "--directory", metavar="DIR", help="use the project in DIR (default: search upwards for project.toml)")
    g.add_argument("--release", action="store_true",
                   help="use the release profile (default: debug)")
    g.add_argument("-v", "--verbose", action="count", default=0,
                   help="print the exact compiler command and have the compiler log the time spent lexing, parsing, "
                        "checking and generating code (-vv adds a per-file table)")
    g.add_argument("-n", "--dry-run", action="store_true", help="print the compiler command without running it")
    g = p.add_argument_group("language options (forwarded to the compiler)")
    g.add_argument("-I", dest="include", action="append", metavar="DIR", help="extra module search directory")
    g.add_argument("--entry", metavar="NAME",
                   help="entry function (default: `entry_fn` from project.toml, normally `main`); `--entry=` builds an object file")
    g.add_argument("--gc", action=argparse.BooleanOptionalAction, default=None,
                   help="conservative garbage collector (default off: memory is owned, see std.own)")
    g.add_argument("--gc-mode", choices=GC_MODES, default=None,
                   help="auto (default): the allocator collects when the nursery is full; thread: a background collector thread "
                        "collects periodically; manual: only gc_collect() / gc_collect_gen(g)")
    g.add_argument("--gc-interval", type=int, default=None, metavar="MS",
                   help="--gc-mode=thread: how often the collector thread wakes up (default 100 ms)")
    g.add_argument("--no-stdruntime", action="store_true", default=None,
                   help="freestanding build: only integer helpers, no libc")
    g.add_argument("--overflow-panic", action=argparse.BooleanOptionalAction, default=None,
                   help="panic on integer overflow (default: on in the debug profile, off in release)")
    g.add_argument("--strict-casts", action="store_true", default=None, help="treat conversion warnings as errors")
    g.add_argument("--no-warn", action="store_true", default=None, help="hide warnings")
    g.add_argument("-X", "--crumbs-arg", action="append", metavar="ARG",
                   help="pass ARG straight to the Crumbs compiler (repeatable)")


def add_cc(p):
    g = p.add_argument_group("C compiler options")
    g.add_argument("-O", dest="opt", choices=OPT_LEVELS, help="C optimization level (default: 0 debug, 3 release)")
    g.add_argument("--unchecked", action=argparse.BooleanOptionalAction, default=None,
                   help="remove bounds, null, division and conversion checks (and overflow panics)")
    g.add_argument("--cc", metavar="CC", help="C compiler (default: $CC, gcc, clang)")
    g.add_argument("--cflag", action="append", metavar="FLAG", help="extra C compiler flag (repeatable)")
    g.add_argument("-D", dest="define", action="append", metavar="NAME[=VALUE]", help="define a preprocessor macro (repeatable)")
    g.add_argument("-U", dest="undef", action="append", metavar="NAME", help="undefine a preprocessor macro (repeatable)")
    g.add_argument("-l", dest="lib", action="append", metavar="NAME", help="link library NAME")
    g.add_argument("-L", dest="libdir", action="append", metavar="DIR", help="library search directory")
    g.add_argument("--link", action="append", metavar="FILE", help="link an object file, archive or shared library")
    g.add_argument("--pkg", action="append", metavar="NAME", help="add flags from pkg-config NAME")
    g.add_argument("--keep-c", nargs="?", const=True, default=None, metavar="PATH",
                   help="also keep the generated C (default path: build/<profile>/<name>.c)")


def create_parser():
    parser = argparse.ArgumentParser(
        prog="cookie", description="The build tool for Crumbs projects.",
        epilog="Settings live in project.toml; run `cookie info` to see the exact compiler commands.\n"
               "Use `cookie <command> --help` for the options of one command.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"cookie {__version__}")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="colored output (default: auto)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p = sub.add_parser("new", help="create a project in a new directory", description="Create a new project in a new directory.")
    p.add_argument("name", help="directory (and project) name")
    p.add_argument("--lib", action="store_true", help="make a library (object file, no entry point) instead of a program")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("init", help="create a project in an existing directory",
                       description="Create project.toml and src/ in a directory (default: the current one).")
    p.add_argument("path", nargs="?", default=".", help="directory (default: .)")
    p.add_argument("--name", help="project name (default: the directory name)")
    p.add_argument("--lib", action="store_true", help="make a library (object file, no entry point) instead of a program")
    p.add_argument("--force", action="store_true", help="overwrite an existing project.toml")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("build", help="compile the project", description="Compile the project to a native executable.")
    add_common(p)
    add_cc(p)
    p.add_argument("-o", "--output", metavar="FILE", help="output file (default: build/<profile>/<name>)")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("run", help="compile and run the project",
                       description="Compile and run. Arguments after `--` go to the program.")
    add_common(p)
    add_cc(p)
    p.add_argument("program_args", nargs="*", metavar="ARGS", help="arguments for the program (after --)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("check", help="type-check without compiling", description="Type-check the project.")
    add_common(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("emit", help="print the generated C", description="Print (or write) the generated C source.")
    add_common(p)
    p.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of standard output")
    p.set_defaults(func=cmd_emit)

    p = sub.add_parser("clean", help="delete the build directory")
    p.add_argument("-C", "--directory", metavar="DIR")
    p.add_argument("-n", "--dry-run", action="store_true", help="only say what would be deleted")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("info", help="show the project and the compiler commands it uses")
    p.add_argument("-C", "--directory", metavar="DIR")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_info)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = create_parser()
    args = parser.parse_args(argv)
    Style.enabled = {"always": True, "never": False}.get(args.color)
    try:
        return args.func(args)
    except CookieError as e:
        return fail(str(e), e.help)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
