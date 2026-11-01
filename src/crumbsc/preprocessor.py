"""The Crumbs preprocessor: `#define` / `#if` / ... applied to source text before lexing.

Design rules
  * Line numbers never change: directive lines, skipped lines and continuation lines become empty lines,
    and a macro expansion stays on the line of its call, so every later diagnostic points at the right line.
  * It is token-aware: strings, char literals and comments are never touched by macro expansion, and a `#`
    inside them is never a directive.
  * `#if` expressions are parsed by a small expression parser (no eval()).
  * Mistakes are reported as diagnostics with a source span, like every other compiler error.

Directives
  #define NAME [body]              object-like macro
  #define NAME(a, b, ...) body     function-like macro; `...` / __VA_ARGS__, __VA_OPT__(x), `#a` (stringify), `a ## b` (paste)
  #undef NAME
  #if / #elif / #else / #endif     integer expressions with defined(X), ! ~ - + * / % << >> < <= > >= == != & ^ | && || ?:
  #ifdef NAME / #ifndef NAME
  #error message / #warning message
  #pragma once                     accepted (every module is loaded once anyway)

Predefined: __FILE__, __LINE__, __COUNTER__, __CRUMBS__, CB_OS_*, CB_ARCH_*, plus whatever -D defines and the
build flags (CB_GC, CB_NO_STDRUNTIME, CB_OVERFLOW_PANIC, CB_UNCHECKED) provide.
"""
import platform
import re

from .diag import Span, Source, suggest

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUMBER = re.compile(r"\.?[0-9](?:[A-Za-z0-9_.]|[eEpP][+-])*")
DIRECTIVES = ("define", "undef", "if", "ifdef", "ifndef", "elif", "else", "endif", "error", "warning", "pragma", "include")
MAX_EXPANSION = 2000             # nested expansions on one line before giving up (runaway macro)

# kinds of token: id, num, str, ws, op, com (comment, kept verbatim)


class Tok:
    __slots__ = ("kind", "text", "col")

    def __init__(self, kind, text, col):
        self.kind = kind
        self.text = text
        self.col = col

    def __repr__(self):
        return f"{self.kind}:{self.text!r}"


class Macro:
    __slots__ = ("name", "params", "variadic", "body", "builtin", "span")

    def __init__(self, name, params, variadic, body, span=None, builtin=False):
        self.name = name
        self.params = params          # None for an object-like macro
        self.variadic = variadic
        self.body = body              # list[Tok]
        self.span = span
        self.builtin = builtin

    def same_as(self, o):
        return (self.params == o.params and self.variadic == o.variadic
                and [t.text for t in self.body if t.kind != "ws"] == [t.text for t in o.body if t.kind != "ws"])


class PPError(Exception):
    def __init__(self, msg, col=1, length=1, label=None, help=None):
        super().__init__(msg)
        self.msg, self.col, self.length, self.label, self.help = msg, col, length, label, help


def tokenize(s, depth=0, base=1):
    """Split one line into tokens. `depth` is the block-comment nesting at the start of the line;
    returns (tokens, depth at the end of the line)."""
    toks, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        start = i
        if depth:                                              # inside /* ... */ (they nest)
            while i < n:
                if s.startswith("/*", i):
                    depth += 1
                    i += 2
                elif s.startswith("*/", i):
                    depth -= 1
                    i += 2
                    if not depth:
                        break
                else:
                    i += 1
            toks.append(Tok("com", s[start:i], start + base))
        elif c in " \t\r\f\v":
            while i < n and s[i] in " \t\r\f\v":
                i += 1
            toks.append(Tok("ws", s[start:i], start + base))
        elif s.startswith("//", i):
            toks.append(Tok("com", s[i:], i + base))
            i = n
        elif s.startswith("/*", i):
            depth = 1
            i += 2
            while i < n and depth:
                if s.startswith("/*", i):
                    depth += 1
                    i += 2
                elif s.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            toks.append(Tok("com", s[start:i], start + base))
        elif c == '"' or c == "'":
            i += 1
            while i < n and s[i] != c:
                i += 2 if s[i] == "\\" else 1
            i = min(n, i + 1)
            toks.append(Tok("str", s[start:i], start + base))
        elif c.isalpha() or c == "_":
            m = IDENT.match(s, i)
            i = m.end()
            toks.append(Tok("id", s[start:i], start + base))
        elif c.isdigit() or (c == "." and i + 1 < n and s[i + 1].isdigit()):
            m = NUMBER.match(s, i)
            i = m.end()
            toks.append(Tok("num", s[start:i], start + base))
        elif s.startswith("##", i) or s.startswith("...", i):
            i += 2 if s[i] == "#" else 3
            toks.append(Tok("op", s[start:i], start + base))
        else:
            i += 1
            toks.append(Tok("op", c, start + base))
    return toks, depth


def untokenize(toks):
    return "".join(t.text for t in toks)


def strip_ws(toks):
    a, b = 0, len(toks)
    while a < b and toks[a].kind in ("ws", "com"):
        a += 1
    while b > a and toks[b - 1].kind in ("ws", "com"):
        b -= 1
    return toks[a:b]


class Preprocessor:
    def __init__(self, diags=None, defines=None, predefined=None):
        """defines: {name: value-text or None}; None undefines a predefined name (-U).
        predefined: {name: value-text} set by the compiler (build flags)."""
        self.d = diags
        self.macros = {}
        self.counter = 0
        self.errors = 0
        self.changed = {}                 # line number -> original text, for lines altered by macros
        for k, v in _platform_defines().items():
            self.set_object(k, v)
        for k, v in (predefined or {}).items():
            self.set_object(k, v)
        for k, v in (defines or {}).items():
            if v is None:
                self.macros.pop(k, None)
            else:
                self.set_object(k, v)

    # ---- macro table -----------------------------------------------------------------------------
    def set_object(self, name, value):
        toks, _ = tokenize(str(value))
        self.macros[name] = Macro(name, None, False, strip_ws(toks))

    def defined(self, name):
        return name in self.macros or name in ("__FILE__", "__LINE__", "__COUNTER__")

    # ---- reporting -------------------------------------------------------------------------------
    def _err(self, src, line, e):
        self.errors += 1
        if self.d is not None:
            self.d.error(Span(src, line, e.col, e.length), e.msg, label=e.label, help=e.help)

    def _warn(self, src, line, col, length, msg, label=None):
        if self.d is not None:
            self.d.warn(Span(src, line, col, length), msg, label=label)

    # ---- the driver ------------------------------------------------------------------------------
    def process(self, text, path="<source>", src=None):
        """Preprocess `text`; returns the new text (same number of lines)."""
        src = src or Source(path, text)
        self.src = src
        self.path = path
        lines = text.split("\n")
        out = []
        depth = 0                                   # block comment nesting carried between lines
        stack = []                                  # conditional groups: [active, taken, seen_else, parent_active, line, col]
        i = 0
        while i < len(lines):
            lineno = i + 1
            raw = lines[i]
            active = all(g[0] for g in stack)
            toks, ndepth = tokenize(raw, depth)
            first = next((t for t in toks if t.kind != "ws"), None)
            if depth == 0 and first is not None and first.kind == "op" and first.text == "#":
                # a directive; a trailing backslash glues the next physical line on
                j = i
                while j + 1 < len(lines) and _ends_with_backslash(lines[j]):
                    j += 1
                logical = [lines[k].rstrip("\r") for k in range(i, j + 1)]
                logical = [l[:-1] if _ends_with_backslash(l) and k < len(logical) - 1 else l
                           for k, l in enumerate(logical)]
                joined = " ".join(logical) if len(logical) > 1 else logical[0]
                self.directive(joined, lineno, first.col, stack, active)
                out.extend([""] * (j - i + 1))
                # a block comment opened on a directive line is rare; the comment text is dropped with the line
                i = j + 1
                depth = 0
                continue
            depth = ndepth
            if not active:
                out.append("")
            else:
                new = self.expand_line(toks, lineno)
                if new != raw.rstrip("\r") and new != raw:
                    self.changed[lineno] = raw
                out.append(new)
            i += 1
        for g in stack:
            self._err(src, g[4], PPError("unterminated conditional directive", g[5], 3,
                                         label="this `#if` is never closed by an `#endif`"))
        return "\n".join(out)

    # ---- directives --------------------------------------------------------------------------------
    def directive(self, text, lineno, col, stack, active):
        toks, _ = tokenize(text)
        toks = [t for t in toks if t.kind != "com"]
        toks = strip_ws(toks)
        # toks[0] is '#'
        rest = strip_ws(toks[1:])
        if not rest:
            return                                              # the null directive
        name_tok = rest[0]
        name = name_tok.text
        args = rest[1:]
        while args and args[0].kind == "ws":
            args = args[1:]
        try:
            if name in ("if", "ifdef", "ifndef"):
                if not active:
                    stack.append([False, True, False, False, lineno, col])
                    return
                if name == "if":
                    try:
                        val = self.eval_if(args, name_tok)
                    except PPError as e:             # keep the nesting intact so later lines are still checked
                        self._err(self.src, lineno, e)
                        val = False
                else:
                    a = strip_ws(args)
                    if not a or a[0].kind != "id":
                        raise PPError(f"`#{name}` needs a macro name", name_tok.col, len(name) + 1)
                    if len(a) > 1:
                        self._warn(self.src, lineno, a[1].col, 1, f"extra tokens after `#{name} {a[0].text}`")
                    val = self.defined(a[0].text)
                    if name == "ifndef":
                        val = not val
                stack.append([val, val, False, True, lineno, col])
            elif name == "elif":
                if not stack:
                    raise PPError("`#elif` without a matching `#if`", name_tok.col, 4)
                g = stack[-1]
                if g[2]:
                    raise PPError("`#elif` after `#else`", name_tok.col, 4)
                parent = all(x[0] for x in stack[:-1])
                if not parent or g[1]:
                    g[0] = False
                else:
                    try:
                        g[0] = self.eval_if(args, name_tok)
                    except PPError as e:
                        self._err(self.src, lineno, e)
                        g[0] = False
                    g[1] = g[0]
            elif name == "else":
                if not stack:
                    raise PPError("`#else` without a matching `#if`", name_tok.col, 4)
                g = stack[-1]
                if g[2]:
                    raise PPError("a second `#else` for the same `#if`", name_tok.col, 4,
                                  label="the group at line %d already has an `#else`" % g[4])
                g[2] = True
                parent = all(x[0] for x in stack[:-1])
                g[0] = parent and not g[1]
                g[1] = True
            elif name == "endif":
                if not stack:
                    raise PPError("`#endif` without a matching `#if`", name_tok.col, 5)
                stack.pop()
            elif not active:
                return
            elif name == "define":
                self.do_define(args, lineno, name_tok)
            elif name == "undef":
                a = strip_ws(args)
                if not a or a[0].kind != "id":
                    raise PPError("`#undef` needs a macro name", name_tok.col, 6)
                self.macros.pop(a[0].text, None)
            elif name == "error":
                raise PPError("#error " + untokenize(args).strip(), name_tok.col, 6)
            elif name == "warning":
                self._warn(self.src, lineno, name_tok.col, 8, "#warning " + untokenize(args).strip())
            elif name == "pragma":
                p = strip_ws(args)
                if not p or p[0].text != "once":
                    self._warn(self.src, lineno, name_tok.col, 7, f"unknown `#pragma {untokenize(p).strip()}` ignored")
            elif name == "include":
                raise PPError("`#include` is not supported", name_tok.col, 8,
                              help="use `import a.b;` for Crumbs modules, or the `include \"header.h\";` directive for a C header")
            else:
                hint = suggest(name, DIRECTIVES)
                raise PPError(f"unknown preprocessor directive `#{name}`", name_tok.col, len(name) + 1,
                              help=f"did you mean `#{hint}`?" if hint else None)
        except PPError as e:
            self._err(self.src, lineno, e)

    def do_define(self, args, lineno, name_tok):
        if not args or args[0].kind != "id":
            raise PPError("`#define` needs a macro name", name_tok.col, 7)
        nm = args[0]
        name = nm.text
        if name in ("defined", "__FILE__", "__LINE__", "__COUNTER__", "__VA_ARGS__", "__VA_OPT__"):
            raise PPError(f"`{name}` cannot be redefined", nm.col, len(name))
        rest = args[1:]
        params, variadic = None, False
        if rest and rest[0].kind == "op" and rest[0].text == "(":          # function-like only when `(` touches the name
            params, variadic = [], False
            k = 1
            expect_param = True
            closed = False
            while k < len(rest):
                t = rest[k]
                k += 1
                if t.kind == "ws":
                    continue
                if t.text == ")":
                    closed = True
                    break
                if t.text == ",":
                    expect_param = True
                    continue
                if t.text == "...":
                    variadic = True
                    params.append("__VA_ARGS__")
                    expect_param = False
                elif t.kind == "id" and expect_param:
                    if t.text in params:
                        raise PPError(f"duplicate macro parameter `{t.text}`", t.col, len(t.text))
                    params.append(t.text)
                    expect_param = False
                    # GNU style `name...` for a named variadic
                    if k < len(rest) and rest[k].text == "...":
                        variadic = True
                        k += 1
                else:
                    raise PPError("bad macro parameter list", t.col, len(t.text),
                                  help="write `#define NAME(a, b) body` or `#define NAME(a, ...)`")
            if not closed:
                raise PPError("macro parameter list is not closed", nm.col, len(name), label="missing `)`")
            body = strip_ws(rest[k:])
        else:
            body = strip_ws(rest)
        m = Macro(name, params, variadic, body, span=(lineno, nm.col))
        old = self.macros.get(name)
        if old is not None and not old.builtin and not old.same_as(m) and old.span is not None:
            self._warn(self.src, lineno, nm.col, len(name), f"`{name}` is redefined",
                       label=f"first defined on line {old.span[0]}; use `#undef {name}` first to silence this")
        self.macros[name] = m
        if params is not None:
            self._check_body(m, nm)

    def _check_body(self, m, nm):
        """`#` must be followed by a parameter; `##` cannot start or end a body."""
        b = [t for t in m.body if t.kind != "ws"]
        if b and b[0].text == "##" or b and b[-1].text == "##":
            raise PPError("`##` cannot be at the start or end of a macro body", nm.col, len(m.name))
        for k, t in enumerate(b):
            if t.text == "#" and t.kind == "op":
                if k + 1 >= len(b) or b[k + 1].text not in m.params:
                    raise PPError("`#` in a macro body must be followed by a parameter name", t.col, 1,
                                  help="`#x` turns the argument x into a string literal")

    # ---- #if expressions ---------------------------------------------------------------------------
    def eval_if(self, toks, name_tok):
        toks = strip_ws([t for t in toks if t.kind != "com"])
        if not toks:
            raise PPError("`#if` with no expression", name_tok.col, 3)
        # `defined X` / `defined(X)` first (the operand must not be macro-expanded), then expand the rest
        pre, k = [], 0
        while k < len(toks):
            t = toks[k]
            if t.kind == "id" and t.text == "defined":
                j = k + 1
                while j < len(toks) and toks[j].kind == "ws":
                    j += 1
                paren = j < len(toks) and toks[j].text == "("
                if paren:
                    j += 1
                    while j < len(toks) and toks[j].kind == "ws":
                        j += 1
                if j >= len(toks) or toks[j].kind != "id":
                    raise PPError("`defined` needs a macro name", t.col, 7)
                nm = toks[j].text
                j += 1
                if paren:
                    while j < len(toks) and toks[j].kind == "ws":
                        j += 1
                    if j >= len(toks) or toks[j].text != ")":
                        raise PPError("missing `)` after `defined(`", t.col, 7)
                    j += 1
                pre.append(Tok("num", "1" if self.defined(nm) else "0", t.col))
                k = j
            else:
                pre.append(t)
                k += 1
        expanded = self.expand(pre, 0, frozenset(), name_tok.col)
        p = _ExprParser([t for t in expanded if t.kind not in ("ws", "com")], name_tok.col)
        return p.parse() != 0

    # ---- expansion ---------------------------------------------------------------------------------
    def expand_line(self, toks, lineno):
        if not any(t.kind == "id" for t in toks):
            return untokenize(toks)
        try:
            return untokenize(self.expand(toks, lineno, frozenset(), 1))
        except PPError as e:
            self._err(self.src, lineno, e)
            return untokenize(toks)

    def expand(self, toks, lineno, disabled, callcol, budget=None):
        budget = budget if budget is not None else [MAX_EXPANSION]
        out = []
        i, n = 0, len(toks)
        while i < n:
            t = toks[i]
            if t.kind != "id":
                out.append(t)
                i += 1
                continue
            name = t.text
            if name == "__LINE__":
                out.append(Tok("num", str(lineno), t.col))
                i += 1
                continue
            if name == "__FILE__":
                out.append(Tok("str", '"' + self.path.replace("\\", "/").replace('"', '\\"') + '"', t.col))
                i += 1
                continue
            if name == "__COUNTER__":
                out.append(Tok("num", str(self.counter), t.col))
                self.counter += 1
                i += 1
                continue
            m = self.macros.get(name)
            if m is None or name in disabled:
                out.append(t)
                i += 1
                continue
            budget[0] -= 1
            if budget[0] < 0:
                raise PPError(f"macro expansion of `{name}` is too deep", t.col, len(name),
                              help="a macro probably expands to itself through another macro")
            if m.params is None:
                body = self.substitute_object(m)
                out.extend(self.expand(body, lineno, disabled | {name}, t.col, budget))
                i += 1
                continue
            # function-like: needs a `(` after optional whitespace; otherwise the name is left alone
            j = i + 1
            while j < n and toks[j].kind in ("ws", "com"):
                j += 1
            if j >= n or toks[j].text != "(" or toks[j].kind != "op":
                out.append(t)
                i += 1
                continue
            args, end = self.collect_args(toks, j, m, t)
            body = self.substitute(m, args, lineno, disabled, t, budget)
            out.extend(self.expand(body, lineno, disabled | {name}, t.col, budget))
            i = end
        return out

    def substitute_object(self, m):
        return self.paste(m.body)

    def collect_args(self, toks, j, m, call):
        """toks[j] is the `(`; returns (list of argument token lists, index after `)`)."""
        depth, k, n = 0, j, len(toks)
        args, cur = [], []
        while k < n:
            t = toks[k]
            if t.kind == "op" and t.text in "([{":
                depth += 1
                if depth > 1:
                    cur.append(t)
            elif t.kind == "op" and t.text in ")]}":
                depth -= 1
                if depth == 0:
                    args.append(cur)
                    k += 1
                    break
                cur.append(t)
            elif t.kind == "op" and t.text == "," and depth == 1:
                args.append(cur)
                cur = []
            else:
                cur.append(t)
            k += 1
        else:
            raise PPError(f"unterminated argument list for macro `{m.name}`", call.col, len(m.name),
                          label="missing `)` (a macro call must fit on one line)")
        args = [strip_ws(a) for a in args]
        want = len(m.params)
        if want == 0 and len(args) == 1 and not args[0]:
            args = []
        if m.variadic:
            fixed = want - 1
            if len(args) < fixed:
                raise PPError(f"macro `{m.name}` needs at least {fixed} argument{'s' if fixed != 1 else ''}, found {len(args)}",
                              call.col, len(m.name))
            va = []
            for a in args[fixed:]:
                if va:
                    va.append(Tok("op", ",", call.col))
                    va.append(Tok("ws", " ", call.col))
                va.extend(a)
            args = args[:fixed] + [va]
        elif len(args) != want:
            raise PPError(f"macro `{m.name}` takes {want} argument{'s' if want != 1 else ''}, found {len(args)}",
                          call.col, len(m.name), label="in this macro call",
                          help=f"defined as `{m.name}({', '.join(p for p in m.params)})`")
        return args, k

    def substitute(self, m, args, lineno, disabled, call, budget):
        pmap = dict(zip(m.params, args))
        body = [t for t in m.body]
        out = []
        i, n = 0, len(body)
        # __VA_OPT__(x): x only if the variadic argument is not empty
        def is_op(k, s):
            return k < n and body[k].kind == "op" and body[k].text == s

        expanded_cache = {}

        def expanded(name):
            if name not in expanded_cache:
                expanded_cache[name] = self.expand(pmap[name], lineno, disabled, call.col, budget)
            return expanded_cache[name]

        while i < n:
            t = body[i]
            if t.kind == "op" and t.text == "#":
                j = i + 1
                while j < n and body[j].kind == "ws":
                    j += 1
                if j < n and body[j].text in pmap:
                    out.append(Tok("str", _stringify(pmap[body[j].text]), t.col))
                    i = j + 1
                    continue
            if t.kind == "id" and t.text == "__VA_OPT__" and m.variadic:
                j = i + 1
                while j < n and body[j].kind == "ws":
                    j += 1
                if j < n and body[j].text == "(":
                    depth, k = 0, j
                    inner = []
                    while k < n:
                        if body[k].text == "(" and body[k].kind == "op":
                            depth += 1
                            if depth > 1:
                                inner.append(body[k])
                        elif body[k].text == ")" and body[k].kind == "op":
                            depth -= 1
                            if depth == 0:
                                break
                            inner.append(body[k])
                        else:
                            inner.append(body[k])
                        k += 1
                    if pmap.get("__VA_ARGS__"):
                        out.extend(inner)
                    i = k + 1
                    continue
            if t.kind == "id" and t.text in pmap:
                # operands of ## are used unexpanded
                prev = _prev_nonws(out)
                nxt = _next_nonws(body, i + 1)
                if (prev is not None and prev.text == "##" and prev.kind == "op") or (nxt is not None and nxt.text == "##" and nxt.kind == "op"):
                    out.extend(pmap[t.text])
                else:
                    out.extend(expanded(t.text))
                i += 1
                continue
            out.append(t)
            i += 1
        return self.paste(out)

    def paste(self, toks):
        """Apply `##`: glue the neighbours into one token (re-tokenised)."""
        if not any(t.kind == "op" and t.text == "##" for t in toks):
            return toks
        out = []
        i, n = 0, len(toks)
        while i < n:
            t = toks[i]
            if t.kind == "op" and t.text == "##":
                while out and out[-1].kind == "ws":
                    out.pop()
                j = i + 1
                while j < n and toks[j].kind == "ws":
                    j += 1
                left = out.pop() if out else None
                right = toks[j] if j < n else None
                glued = (left.text if left else "") + (right.text if right else "")
                new, _ = tokenize(glued)
                out.extend(new)
                i = j + 1
                continue
            out.append(t)
            i += 1
        return out

    # ---- convenience API -----------------------------------------------------------------------------
    def preprocess(self, source_code, path="<source>"):
        return self.process(source_code, path)


def _ends_with_backslash(line):
    return line.rstrip("\r").endswith("\\")


def _prev_nonws(out):
    for t in reversed(out):
        if t.kind != "ws":
            return t
    return None


def _next_nonws(body, k):
    while k < len(body):
        if body[k].kind != "ws":
            return body[k]
        k += 1
    return None


def _stringify(toks):
    text = untokenize(strip_ws(toks))
    text = re.sub(r"\s+", " ", text)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _platform_defines():
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    d = {"__CRUMBS__": "1"}
    d["CB_OS_LINUX" if sysname == "linux" else "CB_OS_MACOS" if sysname == "darwin" else "CB_OS_WINDOWS" if sysname == "windows" else "CB_OS_OTHER"] = "1"
    if machine in ("x86_64", "amd64"):
        d["CB_ARCH_X86_64"] = "1"
    elif machine in ("aarch64", "arm64"):
        d["CB_ARCH_AARCH64"] = "1"
    return d


class _ExprParser:
    """Recursive-descent parser/evaluator for `#if` expressions over Python ints."""
    BIN = [("||",), ("&&",), ("|",), ("^",), ("&",), ("==", "!="), ("<", "<=", ">", ">="), ("<<", ">>"), ("+", "-"), ("*", "/", "%")]

    def __init__(self, toks, col):
        self.items = []
        self.col = col
        # merge multi-character operators out of single-character op tokens
        i = 0
        while i < len(toks):
            t = toks[i]
            if t.kind == "op":
                two = t.text + (toks[i + 1].text if i + 1 < len(toks) and toks[i + 1].kind == "op" and toks[i + 1].col == t.col + 1 else "")
                if two in ("||", "&&", "==", "!=", "<=", ">=", "<<", ">>"):
                    self.items.append(("op", two, t.col))
                    i += 2
                    continue
                self.items.append(("op", t.text, t.col))
            elif t.kind == "num":
                self.items.append(("num", t.text, t.col))
            elif t.kind == "id":
                self.items.append(("id", t.text, t.col))
            elif t.kind == "str":
                self.items.append(("str", t.text, t.col))
            i += 1
        self.pos = 0

    def peek(self):
        return self.items[self.pos] if self.pos < len(self.items) else (None, None, self.col)

    def take(self):
        t = self.peek()
        self.pos += 1
        return t

    def parse(self):
        v = self.ternary()
        if self.pos < len(self.items):
            k, s, c = self.peek()
            raise PPError(f"unexpected `{s}` in `#if` expression", c, len(s))
        return v

    def ternary(self):
        c = self.binary(0)
        if self.peek()[1] == "?":
            self.take()
            a = self.ternary()
            if self.peek()[1] != ":":
                raise PPError("expected `:` in `?:` expression", self.peek()[2], 1)
            self.take()
            b = self.ternary()
            return a if c else b
        return c

    def binary(self, level):
        if level == len(self.BIN):
            return self.unary()
        ops = self.BIN[level]
        v = self.binary(level + 1)
        while self.peek()[0] == "op" and self.peek()[1] in ops:
            _, op, col = self.take()
            if op == "||" or op == "&&":
                r = self.binary(level + 1)
                v = int(bool(v) or bool(r)) if op == "||" else int(bool(v) and bool(r))
                continue
            r = self.binary(level + 1)
            if op in ("/", "%") and r == 0:
                raise PPError("division by zero in `#if` expression", col, 1)
            v = _apply(op, v, r)
        return v

    def unary(self):
        k, s, c = self.peek()
        if k == "op" and s in ("!", "~", "-", "+"):
            self.take()
            v = self.unary()
            return int(not v) if s == "!" else ~v if s == "~" else -v if s == "-" else v
        return self.primary()

    def primary(self):
        k, s, c = self.take()
        if k == "num":
            txt = s.replace("_", "").rstrip("uUlL")
            try:
                if txt.lower().startswith("0b"):
                    return int(txt[2:], 2)
                if txt.lower().startswith("0x"):
                    return int(txt[2:], 16)
                if txt.lower().startswith("0o"):
                    return int(txt[2:], 8)
                return int(txt)
            except ValueError:
                raise PPError(f"`{s}` is not an integer", c, len(s), help="`#if` works on integers only")
        if k == "id":
            return 1 if s == "true" else 0         # unknown names are 0, like C
        if k == "str":
            raise PPError("a string cannot be used in an `#if` expression", c, len(s))
        if k == "op" and s == "(":
            v = self.ternary()
            if self.peek()[1] != ")":
                raise PPError("missing `)` in `#if` expression", c, 1)
            self.take()
            return v
        if k is None:
            raise PPError("`#if` expression ends unexpectedly", self.col, 3)
        raise PPError(f"unexpected `{s}` in `#if` expression", c, len(s))


def _apply(op, a, b):
    if op == "+": return a + b
    if op == "-": return a - b
    if op == "*": return a * b
    if op == "/": return int(a / b) if (a < 0) != (b < 0) and a % b else a // b
    if op == "%": return a - b * (int(a / b) if (a < 0) != (b < 0) and a % b else a // b)
    if op == "<<": return a << b if 0 <= b < 64 else 0
    if op == ">>": return a >> b if 0 <= b < 64 else 0
    if op == "<": return int(a < b)
    if op == "<=": return int(a <= b)
    if op == ">": return int(a > b)
    if op == ">=": return int(a >= b)
    if op == "==": return int(a == b)
    if op == "!=": return int(a != b)
    if op == "&": return a & b
    if op == "^": return a ^ b
    if op == "|": return a | b
    raise PPError(f"unsupported operator `{op}`")


def needs_preprocessing(text):
    """Cheap test: does this text contain a directive or a built-in macro?"""
    return re.search(r"(?m)^[ \t]*#|__(?:LINE|FILE|COUNTER)__", text) is not None
