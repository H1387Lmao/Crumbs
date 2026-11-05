"""The mini-language after the colon in an interpolation: `{value:[[fill]align][sign][#][0][width][,|_][.precision][type]}`.

    fill       any single ASCII character (only when an alignment follows it)
    align      <  left      >  right      ^  centre
    sign       +  always show a sign      -  only for negatives (the default)      (space)  a space for non-negatives
    #          alternate form: 0x / 0X / 0b / 0o prefix (with x X b o)
    0          pad numbers with zeros after the sign / prefix
    width      minimum width in characters
    , or _     digit grouping (`,` for decimal numbers; `_` also for x b o, grouped by 4)
    .precision digits after the point (floats) or maximum length (strings)
    type       d x X b o c  (integers)   f F e E g G  (floats)   s  (text)   ?  (debug representation)
"""
from dataclasses import dataclass

TYPES = "dxXbocfFeEgGs?"
MAX_WIDTH = 1 << 20
MAX_PREC = 1000


class SpecError(Exception):
    def __init__(self, msg, help=None):
        super().__init__(msg)
        self.msg = msg
        self.help = help


@dataclass
class Spec:
    fill: str = ""          # one ASCII character, or "" for the default (space)
    align: str = ""         # "<", ">", "^" or "" for the type's default
    sign: str = ""          # "+", " " or ""
    alt: bool = False
    zero: bool = False
    width: int = 0
    group: str = ""         # "," "_" or ""
    prec: int = -1          # -1: not given
    type: str = ""          # one of TYPES or ""

    @property
    def plain(self):
        return self == Spec()


def parse_spec(t):
    s = Spec()
    n, i = len(t), 0
    if n >= 2 and t[1] in "<>^":
        if not t[0].isascii() or t[0] in "{}":
            raise SpecError(f"invalid fill character `{t[0]}`", "the fill must be a single ASCII character other than `{` and `}`")
        s.fill, s.align, i = t[0], t[1], 2
    elif n >= 1 and t[0] in "<>^":
        s.align, i = t[0], 1
    if i < n and t[i] in "+- ":
        s.sign = "" if t[i] == "-" else t[i]
        i += 1
    if i < n and t[i] == "#":
        s.alt = True
        i += 1
    if i < n and t[i] == "0":
        s.zero = True
        i += 1
    j = i
    while j < n and t[j].isdigit():
        j += 1
    if j > i:
        s.width = int(t[i:j])
        if s.width > MAX_WIDTH:
            raise SpecError(f"the width {s.width} is too large", f"the largest width is {MAX_WIDTH}")
    i = j
    if i < n and t[i] in ",_":
        s.group = t[i]
        i += 1
    if i < n and t[i] == ".":
        j = i + 1
        while j < n and t[j].isdigit():
            j += 1
        if j == i + 1:
            raise SpecError("expected digits after `.` in the format spec", "write the number of digits, e.g. `.2`")
        s.prec = int(t[i + 1:j])
        if s.prec > MAX_PREC:
            raise SpecError(f"the precision {s.prec} is too large", f"the largest precision is {MAX_PREC}")
        i = j
    if i < n and t[i] in TYPES:
        s.type = t[i]
        i += 1
    if i < n:
        raise SpecError(f"unexpected `{t[i:]}` in the format spec `{t}`",
                        "the order is [[fill]align][sign][#][0][width][,|_][.precision][type], e.g. `*^12`, `+08.3f`, `#x`, `,d`")
    return s


def check_for(spec, kind):
    """Check a parsed spec against the kind of value it formats. kind: int, float, bool, text, enum, ptr, other.
    Returns an error message (with help as a tuple) or None."""
    ty = spec.type
    allowed = {"int": "dxXbocfFeEgGs?", "float": "fFeEgGs?", "bool": "s?", "text": "s?", "enum": "ds?", "ptr": "s?", "other": "?"}[kind]
    if ty and ty not in allowed:
        what = {"int": "an integer", "float": "a float", "bool": "a `bool`", "text": "text", "enum": "an enum", "ptr": "a pointer",
                "other": "this type"}[kind]
        good = {"int": "d x X b o c (or f e g to show it as a float)", "float": "f F e E g G", "bool": "s", "text": "s or ?",
                "enum": "s d ?", "ptr": "s", "other": "? (the automatic representation)"}[kind]
        return (f"the format type `{ty}` cannot format {what}", f"valid types here: {good}")
    numeric = kind in ("int", "float")
    if not numeric and kind != "enum":
        if spec.sign:
            return ("a sign flag only applies to numbers", "remove `+` / space from the spec")
        if spec.zero:
            return ("the `0` flag only applies to numbers", "use a fill character instead, e.g. `*>8`")
    if spec.alt and not (kind == "int" and ty in ("x", "X", "b", "o")):
        return ("the `#` flag only applies to the types x, X, b and o", "write e.g. `{n:#x}` for `0xff`")
    if spec.group:
        dec = kind == "float" or (kind == "int" and ty in ("", "d"))
        if spec.group == "," and not dec:
            return ("`,` grouping only applies to decimal numbers", "use `_` to group hex, binary and octal digits")
        if spec.group == "_" and not (dec or (kind == "int" and ty in ("x", "X", "b", "o"))):
            return ("`_` grouping only applies to numbers", None)
    if spec.prec >= 0:
        if kind == "int" and ty not in ("f", "F", "e", "E", "g", "G"):
            return ("a precision does not apply to integers", "to show an integer as a float write e.g. `{n:.2f}`")
        if kind in ("float",) and spec.prec > 100:
            return (f"the precision {spec.prec} is too large for a float", "the largest is 100")
        if kind in ("int",) and spec.prec > 100:
            return (f"the precision {spec.prec} is too large for a float", "the largest is 100")
        if kind in ("bool", "enum", "ptr", "other") and ty != "?":
            return ("a precision only applies to floats and text", None)
    if ty == "c" and (spec.sign or spec.zero or spec.group):
        return ("the `c` type shows a single character, so sign, `0` and grouping do not apply", None)
    return None


def _scan_placeholder(b, i):
    """From just after a `{`: the index of the matching `}` and of the top-level `:` that starts the spec (-1 if none).
    Brackets, string and character literals and `::` are skipped, so `{m.get(\"k\"):>4}` splits where it should."""
    depth, n, colon = 0, len(b), -1
    while i < n:
        c = b[i]
        if c in (34, 39):                               # " or ' : skip the literal
            q = c
            i += 1
            while i < n and b[i] != q:
                i += 2 if b[i] == 92 else 1
        elif c in (40, 91, 123):
            depth += 1
        elif c in (41, 93):
            depth -= 1
        elif c == 125:
            if depth <= 0:
                return i, colon
            depth -= 1
        elif c == 58 and depth == 0 and colon == -1:
            if b[i + 1:i + 2] == b":":
                i += 1
            else:
                colon = i
        i += 1
    return -1, -1


def parse_template(b):
    """Split a Rust-style format string into pieces. `{{` and `}}` are literal braces; `{arg:spec}` is a placeholder whose
    arg is empty (the next positional argument), an index (`{0}`), a name / dotted path (`{total}`, `{p.pos.x}`) or any
    expression (`{a + b}`). Returns a list of ("lit", bytes) and ("arg", ref, spec) where ref is None, an int, a str
    or ("expr", text). Raises SpecError."""
    out, buf, i, n = [], bytearray(), 0, len(b)
    while i < n:
        c = b[i]
        if c == 123:                                   # {
            if b[i + 1:i + 2] == b"{":
                buf.append(123)
                i += 2
                continue
            j, colon = _scan_placeholder(b, i + 1)
            if j == -1:
                raise SpecError("unclosed `{` in the format string", "close it with `}`, or write `{{` for a literal brace")
            inner = b[i + 1:j].decode("utf-8", "replace")
            if colon == -1:
                ref, spec = inner, ""
            else:
                ref, spec = b[i + 1:colon].decode("utf-8", "replace"), b[colon + 1:j].decode("utf-8", "replace")
            ref = ref.strip()
            if buf:
                out.append(("lit", bytes(buf)))
                buf = bytearray()
            if ref == "":
                r = None
            elif ref.isdigit():
                r = int(ref)
            elif all(p.isidentifier() for p in ref.split(".")):
                r = ref
            else:
                r = ("expr", ref)                      # any other expression: `{a + b}`, `{f(x):>5}`, `{xs[0]}`
            out.append(("arg", r, spec))
            i = j + 1
            continue
        if c == 125:                                   # }
            if b[i + 1:i + 2] == b"}":
                buf.append(125)
                i += 2
                continue
            raise SpecError("single `}` in the format string", "write `}}` for a literal brace")
        buf.append(c)
        i += 1
    if buf:
        out.append(("lit", bytes(buf)))
    return out
