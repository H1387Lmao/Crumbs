"""Phase timing for `--verbose`: lexing, parsing, loading, checking, code generation, the C compiler.

Phases may nest (loading a file lexes and parses it); a phase's reported time excludes its children,
so the numbers add up to the total."""
import time
from contextlib import contextmanager


class Timings:
    def __init__(self):
        self.enabled = False
        self.excl = {}          # phase -> exclusive seconds
        self.calls = {}         # phase -> number of times entered
        self.files = []         # (display path, lines, tokens, lex seconds, parse seconds)
        self.stats = {}         # free-form counters ("tokens", "lines", "c_lines", ...)
        self._stack = []
        self.t0 = time.perf_counter()

    @contextmanager
    def phase(self, name):
        if not self.enabled:
            yield
            return
        start = time.perf_counter()
        self._stack.append(0.0)               # time spent in children
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            child = self._stack.pop()
            self.excl[name] = self.excl.get(name, 0.0) + elapsed - child
            self.calls[name] = self.calls.get(name, 0) + 1
            if self._stack:
                self._stack[-1] += elapsed

    def add(self, key, n=1):
        self.stats[key] = self.stats.get(key, 0) + n

    def total(self):
        return time.perf_counter() - self.t0


TIMINGS = Timings()
