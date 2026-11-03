"""Lexer: turns source text into tokens, recovering from errors."""
import re
from .diag import Span

KEYWORDS = {
    "fn", "let", "mut", "const", "if", "else", "while", "loop", "for", "in",
    "return", "break", "continue", "struct", "enum", "import", "as", "pub",
    "match", "true", "false", "extern", "error", "throw", "try", "catch", "export",
}

OPS3 = {"<<=", ">>=", "..=", "..."}
OPS2 = {
    "->", "=>", "==", "!=", "<=", ">=", "&&", "||", "<<", ">>", "+=", "-=",
    "*=", "/=", "%=", "&=", "|=", "^=", "..", "++", "--", "??", "?.",
}
OPS1 = set("+-*/%&|^~!<>=.,;:(){}[]?")

INT_SUFFIXES = {"i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64"}
FLOAT_SUFFIXES = {"f32", "f64"}

NUM_RE = re.compile(
    r"0[xX][0-9a-fA-F_]*|0[bB][01_]*|0[oO][0-7_]*"
    r"|[0-9][0-9_]*(?:\.[0-9][0-9_]*)?(?:[eE][+-]?[0-9_]+)?"
)
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

ESCAPES = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, '"': 34, "'": 39}


class Token:
    __slots__ = ("kind", "val", "span", "suffix")

    def __init__(self, kind, val, span, suffix=None):
        self.kind = kind  # int float str char ident kw op eof
        self.val = val
        self.span = span
        self.suffix = suffix

    def __repr__(self):
        return f"Token({self.kind}, {self.val!r})"


def lex(src, diags):
    toks, i, line, line_start = _lex(src, diags, 0, len(src.text), 1, 0)
    toks.append(Token("eof", None, Span(src, line, i - line_start + 1, 1)))
    return toks


def _lex(src, diags, i, n, line, line_start):
    text = src.text
    toks = []

    def span(start, end, ln=None, ls=None):
        return Span(src, ln or line, start - (line_start if ls is None else ls) + 1, end - start)

    while i < n:
        c = text[i]
        if c == "\n":
            i += 1
            line += 1
            line_start = i
            continue
        if c in " \t\r":
            i += 1
            continue
        # ---- comments
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            depth = 0
            start, sline, sls = i, line, line_start
            while i < n:
                if text.startswith("/*", i):
                    depth += 1
                    i += 2
                elif text.startswith("*/", i):
                    depth -= 1
                    i += 2
                    if depth == 0:
                        break
                else:
                    if text[i] == "\n":
                        line += 1
                        line_start = i + 1
                    i += 1
            if depth != 0:
                diags.error(Span(src, sline, start - sls + 1, 2), "unterminated block comment",
                            label="comment starts here", help="close it with `*/`")
            continue
        # ---- identifiers / keywords
        if c.isalpha() and c.isascii() or c == "_":
            m = IDENT_RE.match(text, i)
            w = m.group()
            kind = "kw" if w in KEYWORDS else "ident"
            toks.append(Token(kind, w, span(i, m.end())))
            i = m.end()
            continue
        # ---- numbers
        if c.isdigit():
            m = NUM_RE.match(text, i)
            body = m.group()
            j = m.end()
            sp_start = i
            suffix = None
            sm = IDENT_RE.match(text, j)
            if sm and not (body[:2].lower() in ("0x",) and False):
                suffix = sm.group()
                j = sm.end()
            sp = span(sp_start, j)
            i = j
            prefix = body[:2].lower()
            clean = body.replace("_", "")
            ok = True
            is_float = False
            if prefix in ("0x", "0b", "0o") and len(body) > 1 and not body[1].isdigit():
                digits = clean[2:]
                if not digits:
                    diags.error(sp, f"missing digits after `{body[:2]}`")
                    ok = False
                else:
                    base = {"0x": 16, "0b": 2, "0o": 8}[prefix]
                    val = int(digits, base)
            else:
                is_float = "." in clean or "e" in clean.lower()
                if is_float:
                    val = float(clean)
                else:
                    val = int(clean)
            if ok and i < n and text[i].isdigit():
                diags.error(span(sp_start, i + 1), f"invalid digit in numeric literal `{body}{text[i]}`")
                ok = False
            if ok and suffix is not None:
                if is_float and suffix not in FLOAT_SUFFIXES:
                    diags.error(sp, f"invalid suffix `{suffix}` for float literal",
                                help="valid suffixes are `f32` and `f64`")
                    ok = False
                elif not is_float and suffix not in INT_SUFFIXES and suffix not in FLOAT_SUFFIXES:
                    diags.error(sp, f"invalid suffix `{suffix}` for integer literal",
                                help="valid suffixes are i8 i16 i32 i64 u8 u16 u32 u64")
                    ok = False
                elif not is_float and suffix in FLOAT_SUFFIXES:
                    val = float(val)
                    is_float = True
            if not ok:
                toks.append(Token("int", 0, sp))
                continue
            if is_float:
                if val == float("inf"):
                    diags.error(sp, "float literal out of range")
                    val = 0.0
                toks.append(Token("float", val, sp, suffix))
            else:
                if val > 0xFFFFFFFFFFFFFFFF:
                    diags.error(sp, "integer literal is too large",
                                help="the largest supported integer is 18446744073709551615 (u64)")
                    val = 0
                toks.append(Token("int", val, sp, suffix))
            continue
        # ---- strings
        if c == '"':
            start, sline, sls = i, line, line_start
            i += 1
            buf = bytearray()
            closed = False
            while i < n:
                ch = text[i]
                if ch == '"':
                    i += 1
                    closed = True
                    break
                if ch == "\n":
                    break
                if ch == "\\":
                    esc_at = i
                    i += 1
                    if i >= n or text[i] == "\n":
                        break
                    e = text[i]
                    if e in ESCAPES:
                        buf.append(ESCAPES[e])
                        i += 1
                    elif e == "x":
                        hx = text[i + 1:i + 3]
                        if len(hx) == 2 and all(h in "0123456789abcdefABCDEF" for h in hx):
                            buf.append(int(hx, 16))
                            i += 3
                        else:
                            diags.error(span(esc_at, min(n, i + 3)), "invalid `\\x` escape",
                                        help="`\\x` must be followed by exactly two hex digits")
                            i += 1
                    else:
                        diags.error(span(esc_at, i + 1), f"unknown escape sequence `\\{e}`",
                                    help="valid escapes: \\n \\t \\r \\0 \\\\ \\\" \\' \\xHH")
                        i += 1
                    continue
                buf += ch.encode("utf-8")
                i += 1
            sp = Span(src, sline, start - sls + 1, max(1, i - start))
            if not closed:
                diags.error(Span(src, sline, start - sls + 1, 1), "unterminated string literal",
                            label="string starts here", help="add a closing `\"` (strings cannot span lines)")
            toks.append(Token("str", bytes(buf), sp))
            continue
        # ---- char literals (a byte, typed u8)
        if c == "'":
            start = i
            i += 1
            val = None
            if i < n and text[i] == "\\":
                i += 1
                e = text[i] if i < n else ""
                if e in ESCAPES:
                    val = ESCAPES[e]
                    i += 1
                elif e == "x" and re.match(r"[0-9a-fA-F]{2}", text[i + 1:i + 3]):
                    val = int(text[i + 1:i + 3], 16)
                    i += 3
                else:
                    diags.error(span(start, min(n, i + 1)), f"unknown escape sequence in char literal")
                    i += 1
                    val = 0
            elif i < n and text[i] not in "'\n":
                ch = text[i]
                i += 1
                if ord(ch) > 255 or len(ch.encode("utf-8")) > 1:
                    diags.error(span(start, i), "char literal must be a single byte",
                                help="use a string for non-ASCII text")
                    val = 0
                else:
                    val = ord(ch)
            if i < n and text[i] == "'" and val is None:
                i += 1
                diags.error(span(start, i), "unterminated or empty char literal", help="char literals look like 'a' or '\\n'")
                val = 0
            elif i < n and text[i] == "'":
                i += 1
            else:
                diags.error(span(start, max(i, start + 1)), "unterminated or empty char literal",
                            help="char literals look like 'a' or '\\n'")
                val = 0 if val is None else val
            toks.append(Token("char", val if val is not None else 0, span(start, i)))
            continue
        # ---- operators
        three, two = text[i:i + 3], text[i:i + 2]
        if three in OPS3:
            toks.append(Token("op", three, span(i, i + 3)))
            i += 3
        elif two in OPS2:
            toks.append(Token("op", two, span(i, i + 2)))
            i += 2
        elif c in OPS1:
            toks.append(Token("op", c, span(i, i + 1)))
            i += 1
        else:
            diags.error(span(i, i + 1), f"unexpected character `{c}`" if c.isprintable() else
                        f"unexpected character U+{ord(c):04X}")
            i += 1
    return toks, i, line, line_start
