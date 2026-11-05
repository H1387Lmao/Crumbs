"""Borrow checker: runs on one type-checked function body (the AST already carries types, locals and move marks).

What it tracks. A *loan* is a borrow of a local variable (or a field/element of one):
  * `&place`            exclusive loan (taking `&` already needs a `let mut` binding, so it is a `&mut` in Rust terms)
  * `place.method()`    with plain `self`: shared loan; with `mut self`: exclusive loan (the checker inserts the `&`)
A pointer-holding expression *carries* the loans of whatever it was made from (a call result carries the loans of its
arguments, a struct literal those of its fields, `let p = &x;` makes `p` hold the loan on `x`).

Rules (non-lexical: a loan lasts until the last use of the variable that holds it):
  1. while a loan on `x` is live, `x` cannot be written, moved or borrowed exclusively; if the loan is exclusive, `x` cannot be read either
  2. one call cannot take two overlapping borrows of the same place when one of them is exclusive
  3. a loan cannot outlive its owner: no returning `&local`, and a pointer stored in a longer-lived variable is an error if
     it is still used after the owner's scope ended
Raw pointers that come out of `alloc`, from a pointer parameter, or through `*p` carry no loans: they stay the unchecked escape hatch.
"""
from .parser import N

OWNING_BORROWS = {"Own.at"}

SKIP = {"span", "ty", "local", "sym", "handler", "orig_args", "eu_ty", "opty", "elem", "region_local", "name_span", "tyn", "targ", "targs"}


class Loan:
    """`kind` is "borrow" for a real loan on a local, or "into" / "through" for what a parameter's value stands for while a
    function is analysed: "into" = the value carries whatever the caller's argument carried (the pointer points into the
    caller's storage), "through" = it carries what the argument's referent holds (a pointer read out of that storage)."""
    __slots__ = ("owner", "path", "excl", "span", "via_own", "closure", "kind")

    def __init__(self, owner, path, excl, span, via_own=False, closure=False, kind="borrow"):
        self.owner, self.path, self.excl, self.span, self.via_own, self.closure, self.kind = owner, path, excl, span, via_own, closure, kind


class ParamOwner:
    """Stands in for the owner of a parameter's referent (it is never accessed or dropped by the function being analysed)."""

    def __init__(self, index, name):
        self.index, self.name, self.glue, self.captured = index, name, False, False


def overlap(a, b):
    n = min(len(a), len(b))
    return a[:n] == b[:n]


class BorrowCk:
    def __init__(self, ck):
        self.ck = ck
        self.d = ck.d

    # ------------------------------------------------------------------ entry
    def run_all(self, entries):
        """Analyse every function body. A function's summary says which of its parameters its result can carry borrows
        from; callers use it instead of assuming the worst. Summaries grow until nothing changes (calls may be mutually
        dependent), then one more pass reports the errors."""
        for ent in entries:
            ent.sym.summary = frozenset()
        for _ in range(6):
            changed = False
            for ent in entries:
                got = self.analyze(ent, report=False) | ent.sym.summary
                if got != ent.sym.summary:
                    ent.sym.summary = got
                    changed = True
            if not changed:
                break
        for ent in entries:
            self.d.context = ent.context
            self.analyze(ent, report=True)
        self.d.context = None

    def analyze(self, ent, report):
        self.report = report
        self.summary = set()
        body, params = ent.body, ent.params
        self.last = {}          # Local -> last tick at which it is used
        self.decl = {}          # Local -> tick of its declaration
        self.loops = []         # (start tick, end tick)
        self.uses = {}          # Local -> [(tick, span)]
        self.addr = 0
        for p in params:
            self.decl[p] = 0
        self.phase = 1
        self.tick = 0
        self.walk(body)
        for loc, uses in list(self.last.items()):
            for s, e in self.loops:
                if self.decl.get(loc, 0) < s and s <= uses <= e:
                    self.last[loc] = max(self.last[loc], e)       # used inside a loop: live to the end of it
        self.phase = 2
        self.tick = 0
        self.holds = {}         # Local -> [Loan] held by that variable
        self.moved = set()
        for i, p in enumerate(params):
            if self.holds_ptr(p.ty):
                self.holds[p] = [Loan(ParamOwner(i, p.name), (), False, p.span, kind="into")]
        self.walk(body)
        sym = ent.sym
        if sym.name in OWNING_BORROWS and sym.mod.name == "std.own":
            # `Own.at` hands out the pointer the owner stores: the result is a borrow of the owner, not of something it was given
            self.summary = {("into", i) if k == "through" else (k, i) for k, i in self.summary}
        return frozenset(self.summary)

    def mark_use(self, loc, span):
        self.last[loc] = self.tick
        self.uses.setdefault(loc, []).append((self.tick, span))

    def next_use(self, loc):
        """Where `loc` is used next after the current point (for error messages)."""
        for t, sp in self.uses.get(loc, []):
            if t > self.tick:
                return sp
        return None

    def err(self, *a, **kw):
        if self.report:
            self.d.error(*a, **kw)

    # ------------------------------------------------------------------ helpers
    def kids(self, n):
        out = []
        for k, v in n.__dict__.items():
            if k in SKIP:
                continue
            for c in (v if isinstance(v, (list, tuple)) else [v]):
                if isinstance(c, N):
                    out.append(c)
                elif isinstance(c, tuple):
                    out.extend(x for x in c if isinstance(x, N))
        return out

    def holds_ptr(self, t):
        return t is not None and not getattr(t, "poison", False) and self.ck.holds_ptr(t)

    def live_after(self, h):
        return self.last.get(h, 0) > self.tick

    def place_root(self, e):
        """(local, path) when place `e` lives inside a local's own storage (not behind a pointer), else None."""
        path = []
        while True:
            k = e.kind
            if k == "Ident":
                loc = getattr(e, "local", None)
                if loc is not None and (getattr(loc, "region_handle", False) or getattr(loc, "captured", False)):
                    return None                     # a closure's captured variable: its owner is the environment
                return (loc, tuple(reversed(path))) if loc is not None else None
            if k == "Field":
                if getattr(e, "mode", None) != "field":
                    return None
                path.append(e.name)
                e = e.obj
                continue
            if k == "Index":
                bt = getattr(e.obj, "ty", None)
                if bt is None or bt.kind != "arr":
                    return None
                path.append("[]")
                e = e.obj
                continue
            return None

    def name_of(self, loc):
        return loc.name

    # ------------------------------------------------------------------ access checks
    def access(self, loc, path, kind, span):
        """`kind`: read | write | move | excl (an exclusive borrow) | shared (a shared borrow)."""
        if self.phase != 2:
            return
        for h, loans in self.holds.items():
            if h is loc or not self.live_after(h):
                continue
            for ln in loans:
                if ln.owner is not loc or not overlap(ln.path, path):
                    continue
                if kind in ("read", "shared") and not ln.excl:
                    continue
                if kind == "move" and ln.via_own:
                    continue                         # the pointer is into the heap block, which does not move with its owner
                self.conflict(loc, kind, ln, h, span)
                return

    def conflict(self, loc, kind, ln, holder, span):
        what = {"read": "use", "write": "assign to", "move": "move out of", "excl": "borrow mutably", "shared": "borrow"}[kind]
        state = "mutably borrowed" if ln.excl else "borrowed"
        how = "mutable" if ln.excl else "shared"
        extra = [(ln.span, f"{how} borrow of `{loc.name}` starts here" + (", held by the closure" if ln.closure else f", and is kept in `{holder.name}`"))]
        later = self.next_use(holder)
        if later is not None:
            extra.append((later, f"`{holder.name}` is used here, so the borrow is still alive"))
        self.err(span, f"cannot {what} `{loc.name}` because it is {state}",
                 label={"read": f"`{loc.name}` is read here", "write": f"`{loc.name}` is assigned here",
                        "move": f"`{loc.name}` is moved here", "excl": f"`{loc.name}` is borrowed mutably here",
                        "shared": f"`{loc.name}` is borrowed here"}[kind] + f" while the {how} borrow is alive",
                 extra=extra,
                 help=f"finish using `{holder.name}` before this, or make a copy of what you need")

    # ------------------------------------------------------------------ the walk
    def walk(self, n):
        """Evaluate node `n`; returns the list of loans its value carries."""
        m = getattr(self, "w_" + n.kind, None)
        if m is not None:
            return m(n)
        carried = []
        for c in self.kids(n):
            carried += self.walk(c)
        return carried if self.holds_ptr(getattr(n, "ty", None)) else []

    def w_Ident(self, n):
        loc = getattr(n, "local", None)
        if loc is None:
            return []
        self.tick += 1
        if self.phase == 1:
            self.mark_use(loc, n.span)
            self.decl.setdefault(loc, self.tick)
            return []
        if getattr(n, "move", False):
            self.access(loc, (), "move", n.span)
            self.moved.add(loc)
        else:
            self.access(loc, (), "read", n.span)
        return list(self.holds.get(loc, [])) if self.holds_ptr(loc.ty) else []

    def w_Field(self, n):
        return self.w_place_read(n)

    def w_Index(self, n):
        return self.w_place_read(n)

    def w_place_read(self, n):
        root = self.place_root(n)
        if root is not None:
            loc, path = root
            self.walk_index_parts(n)
            self.tick += 1
            if self.phase == 1:
                self.mark_use(loc, n.span)
                self.decl.setdefault(loc, self.tick)
                return []
            self.access(loc, path, "move" if getattr(n, "move", False) else "read", n.span)
            return list(self.holds.get(loc, [])) if self.holds_ptr(getattr(n, "ty", None)) else []
        carried = []
        for c in self.kids(n):
            carried += self.walk(c)
        if not self.holds_ptr(getattr(n, "ty", None)):
            return []
        return carried if self.addr else self.read_through(carried)

    def read_through(self, loans):
        """The loans of a value read out of the storage that pointers with `loans` point to."""
        out = []
        for ln in loans:
            if isinstance(ln.owner, ParamOwner):
                out.append(Loan(ln.owner, (), False, ln.span, kind="through"))
            elif ln.kind == "borrow":
                out += self.holds.get(ln.owner, [])
        return out

    def base_ident(self, n):
        while n.kind in ("Field", "Index"):
            n = n.obj
        return n

    def is_chain_part(self, top, c):
        x = top
        while x.kind in ("Field", "Index"):
            if x.obj is c:
                return True
            x = x.obj
        return c is x

    def w_Unary(self, n):
        if n.op == "&":
            root = self.place_root(n.expr)
            if root is not None:
                loc, path = root
                excl = not getattr(n, "auto_ref", False)
                self.walk_index_parts(n.expr)
                self.tick += 1
                if self.phase == 1:
                    self.mark_use(loc, n.span)
                    self.decl.setdefault(loc, self.tick)
                    return []
                self.access(loc, path, "excl" if excl else "shared", n.span)
                return [Loan(loc, path, excl, n.span, via_own=self.is_own(loc.ty))]
        if n.op == "&":
            return self.place_loans(n.expr)
        carried = []
        for c in self.kids(n):
            carried += self.walk(c)
        if not self.holds_ptr(getattr(n, "ty", None)):
            return []
        if n.op == "*":
            return self.read_through(carried)
        return carried

    def place_loans(self, e):
        """The loans that a pointer to place `e` carries when the place lives behind a pointer: those of the pointer."""
        if e.kind in ("Field", "Index"):
            if e.kind == "Index":
                self.walk(e.idx)
            return self.place_loans(e.obj)
        if e.kind == "Unary" and e.op == "*":
            return self.walk(e.expr)
        return self.walk(e)

    def is_own(self, t):
        return t.kind == "struct" and t.sym.name.startswith("Own") and "own" in t.sym.full

    def is_checked(self, t):
        """`std.own` handles that check themselves when used (`Ref`, `Weak`): they carry no loans."""
        return t is not None and t.kind == "struct" and t.sym.name.startswith(("Ref", "Weak")) and "own" in t.sym.full

    def walk_index_parts(self, e):
        while e.kind in ("Field", "Index"):
            if e.kind == "Index":
                self.walk(e.idx)
            e = e.obj

    def w_Call(self, n):
        c = n.callee
        indirect = getattr(n, "indirect", False)
        callee_loans = []
        if indirect:
            callee_loans = self.walk(c)                         # a closure or function pointer value, with whatever it carries
        elif not (getattr(n, "method_recv", False) or c.kind != "Ident" or getattr(c, "local", None) is None):
            self.walk(c)
        per_arg = [self.walk(a) for a in n.args]
        if self.is_checked(getattr(n, "ty", None)):
            return []
        sym = getattr(n, "sym", None)
        if self.phase == 2 and not getattr(sym, "extern", False):      # a C function takes raw pointers: aliasing is its business
            for i in range(len(per_arg)):
                for j in range(i + 1, len(per_arg)):
                    for l1 in per_arg[i]:
                        for l2 in per_arg[j]:
                            if l1.owner is l2.owner and overlap(l1.path, l2.path) and (l1.excl or l2.excl):
                                self.err(l2.span, f"cannot borrow `{l1.owner.name}` twice in one call",
                                         label=f"`{l1.owner.name}` is also borrowed at line {l1.span.line} in this call",
                                         help="borrow it once, or borrow different fields")
        if not self.holds_ptr(getattr(n, "ty", None)):
            return []
        if getattr(sym, "extern", False) and not indirect:
            return []                                           # raw pointers handed out by C
        summary = getattr(sym, "summary", None)
        carried = list(callee_loans)
        if indirect or summary is None or getattr(n, "builtin", None):
            for p in per_arg:                                   # an unknown callee (closure, function pointer, builtin): assume the worst
                carried += p
            return carried
        for kind, i in summary:
            if i < len(per_arg):
                carried += per_arg[i] if kind == "into" else self.read_through(per_arg[i])
        return carried

    def w_Assign(self, n):
        t = n.target
        carried = self.walk(n.value)
        root = self.place_root(t)
        if root is not None:
            loc, path = root
            self.walk_index_parts(t)
            self.tick += 1
            if self.phase == 1:
                self.mark_use(loc, t.span)
                self.decl.setdefault(loc, self.tick)
                return []
            if n.op == "=":
                self.access(loc, path, "write", t.span)
            else:
                self.access(loc, path, "write", t.span)
            if carried and self.holds_ptr(loc.ty):
                self.holds.setdefault(loc, []).extend(carried)
            return []
        # a store behind a pointer, or into a pointer-held place: only the pieces are evaluated
        for c in self.kids(t):
            self.walk(c)
        return []

    def w_IncDec(self, n):
        root = self.place_root(n.expr)
        if root is not None:
            loc, path = root
            self.walk_index_parts(n.expr)
            self.tick += 1
            if self.phase == 1:
                self.mark_use(loc, n.expr.span)
                self.decl.setdefault(loc, self.tick)
                return []
            self.access(loc, path, "write", n.expr.span)
            return []
        self.walk(n.expr)
        return []

    def w_Let(self, n):
        carried = self.walk(n.init) if getattr(n, "init", None) is not None else []
        loc = getattr(n, "local", None)
        if loc is not None:
            if self.phase == 1:
                self.decl[loc] = self.tick
                self.last.setdefault(loc, self.tick)
            elif carried and self.holds_ptr(loc.ty):
                self.holds[loc] = list(carried)
        return []

    def w_Block(self, n):
        start = self.tick
        owners = []
        for st in n.stmts if hasattr(n, "stmts") else self.kids(n):
            self.walk(st)
            if st.kind == "Let" and getattr(st, "local", None) is not None:
                owners.append(st.local)
        if self.phase == 2:
            for x in owners:
                for h, loans in self.holds.items():
                    if h is x or self.decl.get(h, 0) > start or not self.live_after(h):
                        continue
                    for ln in loans:
                        if ln.owner is x:
                            self.outlive(ln, x, h)
                            break
        return []

    def outlive(self, ln, owner, holder):
        if ln.via_own:
            msg = f"pointer into the owned value `{owner.name}` would outlive it if stored here"
            label = f"`{owner.name}` is freed when its scope ends, so this pointer would dangle"
        else:
            msg = f"`{owner.name}` does not live long enough"
            label = f"`{holder.name}` borrows `{owner.name}` here but is used after `{owner.name}` goes out of scope"
        self.err(ln.span, msg, label=label, help="move the owner out instead, or keep the borrow inside this scope")

    def w_Return(self, n):
        if getattr(n, "expr", None) is None:
            return []
        carried = self.walk(n.expr)
        if self.phase == 2:
            for ln in carried:
                if isinstance(ln.owner, ParamOwner):
                    self.summary.add((ln.kind, ln.owner.index))
                    continue
                if ln.owner in self.moved:
                    continue
                if ln.via_own:
                    self.err(ln.span, f"pointer into the owned value `{ln.owner.name}` cannot be returned",
                                 label=f"`{ln.owner.name}` is freed when its scope ends, so this pointer would dangle",
                                 help="move the owner out instead (return it or store it), or keep a checked `Ref` with `.ref()`")
                elif ln.closure:
                    self.err(ln.span, f"this closure borrows the local `{ln.owner.name}` and cannot be returned",
                                 label=f"`{ln.owner.name}` is destroyed when the function returns, so the closure would dangle",
                                 help="write `move |..| ...` to give the closure its own copy")
                else:
                    self.err(ln.span, f"cannot return a reference to the local `{ln.owner.name}`",
                                 label=f"`{ln.owner.name}` is destroyed when the function returns, so this would dangle",
                                 help="return the value itself, or allocate it with `alloc` / `own.new`")
                break
        return []

    def w_While(self, n):
        return self.loop(n)

    def w_For(self, n):
        if getattr(n, "desugared", None) is not None:
            return self.walk(n.desugared)
        return self.loop(n)

    def w_Loop(self, n):
        return self.loop(n)

    def loop(self, n):
        start = self.tick
        loc = getattr(n, "local", None)
        if loc is not None and self.phase == 1:
            self.decl[loc] = self.tick
        for c in self.kids(n):
            self.walk(c)
        if self.phase == 1:
            self.loops.append((start + 1, self.tick))
        return []

    def w_Fn(self, n):
        return []

    def w_Closure(self, n):
        """Creating a closure borrows what it captures by reference (exclusive when the body writes it) and moves what it captures by value."""
        carried = []
        for outer, proxy in n.captures:
            self.tick += 1
            if self.phase == 1:
                self.mark_use(outer, n.span)
                self.decl.setdefault(outer, self.tick)
                continue
            if proxy.byref:
                self.access(outer, (), "excl" if proxy.written else "shared", n.span)
                if not outer.captured:
                    carried.append(Loan(outer, (), proxy.written, n.span, closure=True))
                else:
                    carried += list(self.holds.get(outer, []))
            else:
                self.access(outer, (), "move" if outer.glue else "read", n.span)
                if outer.glue:
                    self.moved.add(outer)
                if self.holds_ptr(outer.ty):
                    carried += list(self.holds.get(outer, []))
        return carried
