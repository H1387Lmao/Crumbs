"""Source tracking and rustc-style diagnostics."""
import os
import sys


class Source:
    def __init__(self, path, text):
        self.path = path
        self.text = text
        self.lines = text.split("\n")


class Span:
    __slots__ = ("src", "line", "col", "length")

    def __init__(self, src, line, col, length=1):
        self.src = src
        self.line = line
        self.col = col
        self.length = max(1, length)

    def join(self, other):
        """Span from the start of self to the end of other (clamped to one line)."""
        if other is not None and other.line == self.line and other.col + other.length > self.col:
            return Span(self.src, self.line, self.col, other.col + other.length - self.col)
        text = self.src.lines[self.line - 1] if self.line - 1 < len(self.src.lines) else ""
        return Span(self.src, self.line, self.col, max(1, len(text) - self.col + 1))

    def __repr__(self):
        return f"{self.src.path}:{self.line}:{self.col}"


class CompileError(Exception):
    """Raised to abort compilation after diagnostics have been recorded."""


class Diag:
    def __init__(self, level, span, msg, label=None, help=None, notes=None):
        self.level = level
        self.span = span
        self.msg = msg
        self.label = label
        self.help = help
        self.notes = notes or []


class Diagnostics:
    def __init__(self):
        self.items = []
        self.context = None   # extra note attached to diagnostics (e.g. generic instantiation)

    def _dup(self, level, span, msg):
        return any(d.level == level and d.msg == msg and d.span.line == span.line and d.span.col == span.col
                   and d.span.src is span.src for d in self.items)

    def has_error_on(self, span):
        return any(d.level == "error" and d.span.line == span.line and d.span.src is span.src for d in self.items)

    def error(self, span, msg, label=None, help=None, notes=None):
        if not self._dup("error", span, msg):
            self.items.append(Diag("error", span, msg, label, help, (notes or []) + ([self.context] if self.context else [])))

    def warn(self, span, msg, label=None, help=None, notes=None):
        if not self._dup("warning", span, msg):
            self.items.append(Diag("warning", span, msg, label, help, (notes or []) + ([self.context] if self.context else [])))

    @property
    def error_count(self):
        return sum(1 for d in self.items if d.level == "error")

    @property
    def has_errors(self):
        return self.error_count > 0

    def render(self, color=None):
        if color is None:
            color = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None

        def paint(s, code):
            return f"\x1b[{code}m{s}\x1b[0m" if color else s

        out = []
        # stable order: by file, line, col
        items = sorted(
            enumerate(self.items),
            key=lambda p: (p[1].span.src.path, p[1].span.line, p[1].span.col, p[0]),
        )
        for _, d in items:
            tint = "1;31" if d.level == "error" else "1;33"
            out.append(paint(d.level, tint) + paint(": " + d.msg, "1"))
            sp = d.span
            gw = len(str(sp.line))
            pad = " " * gw
            out.append(f"{pad}{paint('-->', '1;34')} {sp.src.path}:{sp.line}:{sp.col}")
            out.append(f"{pad} {paint('|', '1;34')}")
            raw = sp.src.lines[sp.line - 1] if sp.line - 1 < len(sp.src.lines) else ""
            shown = raw.expandtabs(4)
            start = len(raw[: sp.col - 1].expandtabs(4))
            width = len(raw[: sp.col - 1 + sp.length].expandtabs(4)) - start
            width = max(1, min(width, max(1, len(shown) - start)))
            out.append(f"{paint(str(sp.line), '1;34')} {paint('|', '1;34')} {shown}")
            carets = " " * start + "^" * width
            lab = (" " + d.label) if d.label else ""
            out.append(f"{pad} {paint('|', '1;34')} {paint(carets + lab, tint)}")
            for n in d.notes:
                out.append(f"{pad} {paint('=', '1;34')} {paint('note', '1')}: {n}")
            if d.help:
                out.append(f"{pad} {paint('=', '1;34')} {paint('help', '1')}: {d.help}")
            out.append("")
        return "\n".join(out)


def edit_distance(a, b):
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def suggest(name, candidates):
    """Return the closest candidate to `name`, or None."""
    best, best_d = None, 10**9
    for c in candidates:
        if c == name:
            continue
        d = edit_distance(name.lower(), c.lower())
        if d < best_d:
            best, best_d = c, d
    limit = max(1, min(3, len(name) // 3 + 1))
    return best if best is not None and best_d <= limit else None
