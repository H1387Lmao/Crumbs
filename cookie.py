#!/usr/bin/env python3

import argparse
import os
import subprocess
import sys
from pathlib import Path

CRUMBS_DIR = Path(__file__).resolve().parent
CRUMBS_FILE = CRUMBS_DIR / "crumbs.py"

DEFAULT_SOURCE_DIR = "src"
DEFAULT_ENTRY = "main.cb"
DEFAULT_BUILD_DIR = "build"


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


def supports_color():
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ

def paint(value, *styles):
    if not supports_color():
        return str(value)

    return "".join(styles) + str(value) + Colors.RESET


def log(log_type, message, suggestion=None, color=Colors.BLUE):
    prefix = paint(f"{log_type.upper()}", Colors.BOLD, color)
    padding = " " * max(1, 10 - len(log_type))

    print(f"[{prefix}]{padding}{message}")

    if suggestion:
        print(
            f"{' ' * 11}{paint(suggestion, Colors.DIM, Colors.GRAY)}"
        )


def info(message, suggestion=None):
    log("info", message, suggestion, Colors.BLUE)


def success(message, suggestion=None):
    log("success", message, suggestion, Colors.GREEN)


def warning(message, suggestion=None):
    log("warning", message, suggestion, Colors.YELLOW)


def error(message, suggestion=None):
    prefix = paint("error", Colors.BOLD, Colors.RED)
    print(f"[{prefix}]    {message}", file=sys.stderr)

    if suggestion:
        print(
            f"{' ' * 11}{paint(suggestion, Colors.DIM, Colors.GRAY)}",
            file=sys.stderr,
        )


def load_toml(path):
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            raise RuntimeError(
                "Python 3.11+ is required, or install tomli."
            )

    with path.open("rb") as file:
        return tomllib.load(file)


def load_config(cwd):
    config_file = cwd / "project.toml"

    if not config_file.is_file():
        raise FileNotFoundError(
            "project.toml not found.",
        )

    return load_toml(config_file)


def get_crumbs(config):
    path = Path(config.get("crumbs_path", CRUMBS_DIR))

    if not path.is_absolute():
        path = CRUMBS_DIR / path

    return path / "crumbs.py"


def get_entry(config, cwd):
    project = config.get("project", {})

    source_dir = project.get("source_dir", DEFAULT_SOURCE_DIR)
    entry = project.get("entry", DEFAULT_ENTRY)

    return cwd / source_dir / entry


def get_build_dir(config, cwd):
    return cwd / config.get("build_dir", DEFAULT_BUILD_DIR)


def validate_project(config, cwd):
    crumbs = get_crumbs(config)
    entry = get_entry(config, cwd)

    if not crumbs.is_file():
        raise FileNotFoundError(
            f"Crumbs compiler not found: {crumbs}"
        )

    if not entry.is_file():
        raise FileNotFoundError(
            f"Entry source file not found: {entry}"
        )

    return crumbs, entry


def execute(command, cwd):
    try:
        return subprocess.run(
            command,
            cwd=cwd,
        ).returncode

    except KeyboardInterrupt:
        warning("Process interrupted.")
        return 130

    except OSError as exc:
        error(f"Could not execute Crumbs: {exc}")
        return 1


# ---------------------------------------------------------
# Init
# ---------------------------------------------------------

def init(args):
    target = Path(args.path).expanduser().resolve()

    if target.exists() and not target.is_dir():
        error(
            f"'{target}' is not a directory."
        )
        return 1

    if target.exists() and any(target.iterdir()):
        if not args.force:
            error(
                f"Directory '{target}' is not empty.",
                "Use --force to initialize anyway."
            )
            return 1

    try:
        target.mkdir(parents=True, exist_ok=True)

        project_name = target.name

        template = f'''[project]
name = "{project_name}"
source_dir = "{DEFAULT_SOURCE_DIR}"
entry = "{DEFAULT_ENTRY}"

crumbs_path = "{CRUMBS_DIR.as_posix()}"
build_dir = "{DEFAULT_BUILD_DIR}"
'''

        config_file = target / "project.toml"

        config_file.write_text(
            template,
            encoding="utf-8",
        )

        src = target / DEFAULT_SOURCE_DIR
        src.mkdir(parents=True, exist_ok=True)

        entry = src / DEFAULT_ENTRY

        if not entry.exists():
            entry.write_text(
                """// Entry
fn main() -> void{
    println(\"Hello world from Crumbs!\");
}
""",
                encoding="utf-8",
            )

    except OSError as exc:
        error(f"Initialization failed: {exc}")
        return 1

    success(f"Initialized Crumbs project '{project_name}'")
    info(f"Location: {target}")
    info("Created project.toml and src/main.cb")

    return 0


def run(args):
    cwd = Path.cwd()

    try:
        config = load_config(cwd)
        crumbs, entry = validate_project(config, cwd)

    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        error(str(exc))
        return 1

    command = [
        sys.executable,
        str(crumbs),
        "run",
        str(entry),
        *args.crumbs_args,
    ]

    info(f"Running {entry.relative_to(cwd)}")

    return execute(command, cwd)


def build(args):
    cwd = Path.cwd()

    try:
        config = load_config(cwd)
        crumbs, entry = validate_project(config, cwd)

    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        error(str(exc))
        return 1

    build_dir = get_build_dir(config, cwd)
    output = build_dir / entry.stem

    try:
        build_dir.mkdir(parents=True, exist_ok=True)

    except OSError as exc:
        error(f"Could not create build directory: {exc}")
        return 1

    if args.keep_c:
        args.crumbs_args.append(
            "--keep-c="+str(output)+".c"
        )

    command = [
        sys.executable,
        str(crumbs),
        "build",
        *args.crumbs_args,
        str(entry),
        "-o",
        str(output),
    ]

    info(f"Building {entry.relative_to(cwd)}")
    # info(f"Running: {" ".join(command)}")

    result = execute(command, cwd)

    if result != 0:
        error(f"Build failed with exit code {result}")
        return result

    success(f"Built {output}")

    return 0


def create_parser():
    parser = argparse.ArgumentParser(
        prog="cookie",
        description="Build system for the Crumbs programming language.",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    # cookie init

    init_parser = subparsers.add_parser(
        "init",
        help="Initialize a Crumbs project.",
    )

    init_parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Directory to initialize (default: current directory).",
    )

    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Initialize even if the directory is not empty.",
    )

    init_parser.set_defaults(func=init)

    # cookie run

    run_parser = subparsers.add_parser(
        "run",
        help="Run the project's entry point.",
    )

    run_parser.set_defaults(func=run)

    # cookie build

    build_parser = subparsers.add_parser(
        "build",
        help="Build the project's entry point.",
    )

    build_parser.add_argument(
        "--keep-c",
        action="store_true",
        help="Keep the generated C file.",
    )

    build_parser.set_defaults(func=build)

    return parser


def main():
    parser = create_parser()

    argv = sys.argv[1:]

    # Explicitly separate Cookie arguments from Crumbs arguments.
    if "--" in argv:
        separator = argv.index("--")
        cookie_args = argv[:separator]
        crumbs_args = argv[separator + 1:]
    else:
        cookie_args = argv
        crumbs_args = []

    args, unknown = parser.parse_known_args(cookie_args)

    # Forward unknown arguments to Crumbs.
    if args.command in ("run", "build"):
        args.crumbs_args = unknown + crumbs_args

    elif unknown or crumbs_args:
        parser.error(
            f"unrecognized arguments: {' '.join(unknown + crumbs_args)}"
        )

    try:
        return args.func(args)

    except KeyboardInterrupt:
        warning("Interrupted.")
        return 130

    except (OSError, ValueError) as exc:
        error(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
