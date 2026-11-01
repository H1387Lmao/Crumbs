# Crumbs
<p align="center">
  <img src="docs/resources/crumbs-big.png" alt="Crumbs programming language logo" width="220" />
</p>

<p align="center">
  <img src="https://img.shields.io/badge/4%2C000%2B-Lines%20of%20code-white?style=flat&labelColor=black&logo=github" alt="5,000+ lines of code" />
  <img src="https://img.shields.io/badge/Written%20in-Python-yellow?style=flat&labelColor=blue&logo=python&logoColor=white&logoSize=auto" alt="Written in Python" />
</p>

A small, statically typed, C-style toy programming language that compiles to C.

Crumbs is implemented in Python, with **5,500+ lines of code**, including approximately
**4,800 lines of Python**. It compiles to a single C source file, which is then built
with GCC or Clang using `-O2`.

```
python3 crumbs.py run   examples/hello.cb        # compile + run
python3 crumbs.py build examples/hello.cb -o hi  # native executable
python3 crumbs.py emit  examples/hello.cb        # print the generated C
python3 crumbs.py check examples/hello.cb        # type-check only
        --unchecked        # drop bounds/null/division/conversion checks (about 1.08x of hand-written C vs 1.3x checked)
        --overflow-panic   # panic on integer overflow (+ - * neg); the default is to wrap around like C
        --no-gc            # plain malloc instead of the (default) garbage collector -- you must free()
        --no-stdruntime    # freestanding build: only integer/bounds helpers, no libc (no print, String, alloc, std.*)
        -O3 --cc clang --keep-c out.c
        --entry=start  # use `fn start()` as the entry point instead of `main`
        --entry=       # no entry point: emits an object file (.o) for linking into another program (use `export fn`)
        -v / -vv       # log time spent lexing, parsing, checking, generating C, plus the C compiler command
python3 tests/run_tests.py                     # test suite
```

### cookie: the build tool

`cookie` builds whole projects. Every option has the same name as the compiler flag it forwards, and
`project.toml` supplies the defaults (command line > profile > `[build]` > built-in default).

```
python3 cookie.py new my_game          # create ./my_game (project.toml, src/main.cb, .gitignore); --lib for a library
python3 cookie.py init                 # turn the current directory into a project
python3 cookie.py run                  # compile and run (debug profile); program arguments go after `--`
python3 cookie.py build --release      # -> build/release/<name>
python3 cookie.py build --no-gc -O2 --overflow-panic --unchecked --cc clang -l m --pkg sdl2 --entry=start
python3 cookie.py check | emit | clean | info [--json]
python3 cookie.py build -n -v          # -n prints the exact compiler command, -v also logs phase timings (-vv per file)
```

```toml
[project]
name = "my_game"
version = "0.1.0"
source_dir = "src"          # entry = "main.cb"
entry_fn = "main"           # "" builds an object file
build_dir = "build"

[build]                     # gc, no_stdruntime, strict_casts, no_warn, cc, include, libs, lib_dirs, link, pkg, cflags
gc = true

[profile.debug]             # opt = "0", overflow_panic = true by default
[profile.release]           # opt = "3"
```

Unknown keys, wrong types and unknown profiles are reported with a suggestion. The compiler is found next to `cookie.py`
(override with `crumbs_path` in `[project]` or the `CRUMBS` environment variable); `project.toml` is searched for upwards from
the current directory (or use `-C DIR`).

## Performance

Benchmarks show that Crumbs produces binaries with sizes comparable to equivalent
C programs. Runtime performance is also close to C, with Crumbs benchmarks taking
approximately **1.2% more time** than their C equivalents in the measured tests.

## Language tour

```
import std.math;                 // modules: one file = one module, no headers
import std.conv;

pub struct Point { x: f64, y: f64 }
enum Color { Red, Green }
error NotFound;                  // error values
const N: i32 = 10;

fn area(p: Point) -> f64 { return p.x * p.y; }

fn lookup(id: i32) -> i32! {     // `T!` = a T or an error
    if id < 0 { throw NotFound; }
    return id * 2;
}

fn main() -> void! {
    let mut total = 0;           // immutable by default
    for i in 0..N { total += i; }                 // 0..N, 0..=N, arrays, slices, strings
    let v = try lookup(3);                         // propagate
    let w = lookup(-1) catch 0;                    // default value
    lookup(-1) catch |e| { println("failed: ", e); return; };   // handler must diverge
    let a = [1, 2, 3];  let s = a[1..];            // arrays and slices
    let buf = alloc(i64, 100);  free(buf);         // heap slice
    match Color.Red { Color.Red => { } Color.Green => { } }     // exhaustive
}
```

**Generics** (monomorphized, like C++ templates: each use with new types gets its own specialised C code, zero overhead):
```
struct Pair<A, B> { first: A, second: B }
fn max<T>(a: T, b: T) -> T { if a > b { return a; } return b; }

let m = max(3, 9);                    // T inferred from arguments
let z = max<i64>(5, 6);               // or explicit
let p = Pair { first: 1, second: "x" };            // struct args inferred from fields
let l: list.List<i32> = list.new();               // or from the expected type
```
Bodies are checked per instantiation (errors carry a note saying which types were used), so there are no
trait bounds: `a > b` simply must work for the chosen `T`. Write `0 as T` for numeric zero. `std.list` (growable
`List<T>`) and `std.mem` (`swap`, `min`, `max`, `sort`) are generic.

**Types:** `i8..i64 u8..u64 f32 f64 bool str String void`, `*T`, `[N]T`, `[]T`, structs, enums, `T!`.
`str` is an immutable byte-string view `{ptr,len}`; char literals (`'a'`) are `u8`.

**Strings.** `str` is a view (literals, slices). `String` is an owned, growable, heap-allocated string
`{ptr,len,cap}` (malloc/realloc) with a destructor: it is freed automatically at the end of its scope.
```
let mut s = string_new();            // or string_from("text")
s += "hello"; s += ' ';              // += appends a str, a String or a single u8
let t = s + " world" + "!";          // one allocation per chain; String if any operand is a String
println(s.len, s[0], s[0..5]);       // .len .cap, indexing -> u8, slicing -> str view
fn takes(x: str) {}  takes(s);       // String converts to str implicitly (arguments, returns, annotated lets)
let u = t;                           // moves t (a String has one owner); `t` cannot be used afterwards
free(s);                             // optional: free early (this moves `s`)
```
`str + str` also works and yields a `str` backed by a bump arena (never freed, appends are amortised O(1));
`to_str(x)` converts numbers/bool/enums to `str`. `==` / `!=` compare any mix of `str` and `String`.
A `String` is moved, not copied (see Destructors); `let v: str = s;` and passing `s` where a `str` is expected only *borrow* it,
so a `str` view must not outlive its `String` (returning a view of a local `String` is a compile error). Temporary `String`s
(`f() + "x"` used only to print or compare) are freed at the end of the statement.

**Pointers.** `*T` may be null: `let p: *Node = null;` (`null` takes its type from context, or write `null as *T`).
Dereferencing null panics with a location. Casts: `p as u64` / `n as *T` (any integer width) and `p as *U`
reinterpret pointers; `0 as *T` is also a null pointer.

**I/O (`std.io`).** `io.getc() -> u8!` (fails with `io.Eof`), `io.putc(c: u8)`,
`io.get_line() -> String!` (a line ends at `\n`, `\r`, `\r\n` or Ctrl+D; `io.Eof` only if nothing was read),
`io.flush()`.

**Numeric conversions.** Numbers convert implicitly where that is safe, with guard rails where it is not.
Integer literals adapt to their context (`let x: u8 = 200`); `as` is always available for an explicit (truncating) cast.

| situation | result |
|---|---|
| lossless widening (`u16 -> i32`, `i32 -> i64`, `i32 -> f64`, `f32 -> f64`) | silent |
| binary operators on different numeric types | both sides go to a common type that holds every value of both (`u8+i8 -> i16`, `u32+i32 -> i64`, int+float -> the float) |
| narrowing or sign-changing (`i32 -> u8`, `i64 -> i32`) at `=`, `let`, arguments, returns, `+=` | allowed, **range-checked at runtime** (panics if the value does not fit) + warning |
| ordering comparison mixing signed and unsigned (`u16 < i32`) | warning |
| bitwise / wrapping op mixing signed and unsigned | warning |
| `i32/i64/u64 -> f32/f64` that can lose precision, `f64 -> f32` | warning |
| unsigned `< 0` / `>= 0` | warning (always false / always true) |
| a constant that does not fit the target (`let q: u8 = BIG;`) | error |
| float -> integer, bool/enum/pointer/String <-> number | error (use `as`) |
| `i64`/`u64` mixed in one operator (no common type) | error |

`--strict-casts` turns every warning in that table into an error.

**Integer overflow.** `+ - *` and unary `-` (and `+= -= *=`, `++`, `--`) wrap around in two's complement, like C: `255u8 + 1`
is `0`. Build with `--overflow-panic` to make overflow a panic with a location instead (`cookie`'s debug profile does this).
Division by zero and `MIN / -1` stay defined: zero panics; `MIN / -1` wraps (panics with `--overflow-panic`).

**`try { } catch { }` blocks.** One handler for a whole group of calls. Inside the block any call to a function returning `T!` (and
any `throw`) that fails jumps to the handler, so the calls need no `try` of their own. Locals of the block are destroyed first;
`return`, `break`, `continue` and nested blocks work normally, and the handler may `throw e` to pass the error on. The binder is optional.

```
try {
    let text = fs.read_to_string("config.txt");   // fails -> handler
    let n = parse(text);                          // fails -> handler
    println("ok ", n);
} catch |e| {
    println("could not load: ", e == fs.NotFound);
}
```

**Increment / decrement.** `i++`, `i--`, `++i`, `--i` work as statements and as expressions
(`cells[p++] = x`) on any mutable integer/float place.

**Methods (`impl`).** Functions can live on a type. `self` borrows the value; `mut self` is for methods that change it, so the
variable must be `let mut`. `Self` names the type inside the block, and a field that holds a function pointer wins over a method of the
same name.

```
struct Point { x: i32, y: i32 }
impl Point {
    pub fn new(x: i32, y: i32) -> Point { return Point { x: x, y: y }; }   // Point.new(1, 2)
    pub fn sum(self) -> i32 { return self.x + self.y; }                     // p.sum()
    pub fn shift(mut self, d: i32) { self.x += d; self.y += d; }            // p.shift(1)
}
struct Pair<T> { a: T, b: T }
impl<T> Pair<T> { pub fn first(self) -> T { return self.a; } }
```

Methods work on temporaries too (`Duration.from_ms(5).to_string()`), on pointers (`p.sum()` with `p: *Point`), and on built-in
types: `impl String { ... }`, `impl str { ... }`. `String`, `str` and `List<T>` have their methods without any import:

```
let mut a = String.new("  Hello  ");
a = a.strip();                     // String methods that return text return a new String
a.push('!');  a.append(" ok");     // mut self
println(a.upper(), " ", a.starts_with("He"), " ", a.replace("l", "L"));   // every `str` function works on a String
let mut xs: List<str> = List.new();
xs.push("Hi");  xs.push("there");
println(xs.len, " ", xs.get(1), " ", str.join(xs.view(), "+"));
```

**`mut` parameters.** Parameters are immutable unless declared `mut`; `fn f(mut n: i32) { n--; }` gives the function
its own mutable copy (the caller's value is unchanged).

**Function pointers.** `fn(i32, i32) -> i32` is a type. Named functions are values (`let f = add;`), can be stored in
arrays and struct fields, passed around, compared and set to `null`; call them like any function: `ops.apply(3, 4)`,
`table[i](x)`. Calling a `null` function pointer panics. There are no closures: a callback that needs state takes a
`*void` user-data argument. Function pointers convert to and from `*void` with `as`.

```
struct Ops { name: str, apply: fn(i32, i32) -> i32 }
let ops = Ops { name: "add", apply: add };
println(ops.apply(3, 4));
```

**Destructors.** `drop TypeName(self) { ... }` (next to the struct; `self` is a `*TypeName`) runs when a value goes out of scope:
at the end of its block, on `return`/`break`/`continue`, and when `try` propagates an error. Values with destructors are
moved, never copied: `let b = a;`, `f(a)` and `return a;` move `a`, and using it afterwards is a compile error
(`use of moved value`). Moves are tracked through `if`, `match` and loops; a variable moved on only some paths gets a
runtime flag so the destructor still runs exactly once. You cannot move out of a field, element or pointer
(borrow with `&` instead). Assigning to a variable or field destroys the old value first, a discarded temporary is destroyed
at once, `drop(x)` destroys a value early, and struct fields are destroyed after the struct's own destructor.
`String` has a built-in destructor, so it follows the same move rules, and structs and containers holding Strings free them.
Raw storage helpers for container authors: `write_at(p, v)` (store without destroying the old value), `read_at(p)`
(take ownership of what `p` points to) and `drop_at(p)`. Panics and `exit` skip destructors.

**C interop.** Everything needed to wrap a C library lives in the source file, so there are no headers and no build scripts:

```
include "SDL.h";                    // #include in the generated C: only needed for macros/constants
link "SDL2";                        // -lSDL2        (also: link pkg "sdl2";  link "libfoo.so";  link "shim.o";  link "glue.c";)
define "SDL_MAIN_HANDLED";          // #define before the headers are included

extern const SDL_INIT_VIDEO: u32;   // a C macro or constant, evaluated in C
extern struct Rect { x: i32, y: i32, w: i32, h: i32 }   // C layout; arrays, nested structs, pointers and fn pointers are fine
extern struct Window;               // opaque: usable only behind a pointer (*Window)
extern fn SDL_Init(flags: u32) -> i32;
extern fn SDL_CreateWindow(title: *u8, x: i32, y: i32, w: i32, h: i32, flags: u32) -> *Window;
extern "SDL_GetError" fn get_error() -> *u8;     // bind under another name
extern fn printf(fmt: *u8, ...) -> i32;          // variadic C functions
export fn on_event(code: i32) -> i32 { ... }     // a Crumbs function with a plain C name, callable from C
```

How it works: every `extern fn` gets a private prototype bound to the real symbol (`__asm__` label), and every `extern struct`
is an ordinary Crumbs struct with C layout, so none of it can clash with the types in a real header. Linked files
(`.o`, `.a`, `.so`/`.dylib`, `.c`) are resolved relative to the file containing the `link`; shared libraries get an rpath
so the executable finds them. The same can be given on the command line: `-l NAME`, `-L DIR`, `--link FILE`, `--pkg NAME`.
Only types with a C equivalent cross the boundary (numbers, `bool`, enums, pointers, function pointers, `extern`/plain structs
of those, arrays of those). Bridge helpers: `cstr(s)` (NUL-terminated copy of a `str`/`String`), `from_cstr(p)` (view of a C string),
`s.ptr` (data pointer of a slice/`str`/`String`), `slice_of(p, n)` (slice over C memory), `size_of(T)`, and `*void` (every pointer
converts to it implicitly; get a typed pointer back with `as`). `examples/ffi/` wraps a small C library the way you would wrap
SDL or raylib: raw declarations, a struct whose `drop` closes the handle, and a callback trampoline.

**`std.list`: a growable array for every element type.** `list.List<T>` works for numbers, `str`, `String`, structs, arrays,
function pointers and nested lists. It owns its elements and is released automatically (running element destructors and freeing
`String`s). API: `new`, `with_capacity`, `from_slice`, `push`, `pop` (`T!`, `Empty`), `at` (pointer, any `T`), `get`, `set`, `insert`, `remove`,
`swap_remove`, `extend`, `view`, `clear`, `release`, `reverse`, `sort_by(l, cmp)` (heap sort, `cmp` like C's `qsort`), `for_each`, `find_by`.

**Garbage collector (on by default; `--no-gc` for plain malloc).** A conservative, single-threaded mark-and-sweep collector (Linux; glibc or musl) manages `alloc()`,
`String` buffers and the string arena. It scans the stack, registers and data segment, follows pointers into the middle of blocks, and
never scans blocks it knows hold no pointers (numbers, bytes). Collection is triggered by allocation volume; `gc_collect()` forces
one, `gc_bytes()` reports the bytes in use and `gc_collections()` counts the collections run (all three are no-ops / 0 with `--no-gc`). `free()` and destructors stay legal and release
memory immediately, so code works the same with or without the collector. Caveats: memory given to C and kept only on the C side is
not seen (keep a Crumbs reference to it); under AddressSanitizer use `ASAN_OPTIONS=detect_stack_use_after_return=0`.

**Lifetimes: `region` blocks.** A region is a scoped arena. `alloc_in(T, n, r)` returns a zeroed `[]T` that lives exactly as long
as `region r { ... }`; the whole region is released in one step on *every* way out (end of block, `break`, `continue`, `return`).
Its memory is never traced or swept one object at a time, so allocating in a region does not wake the collector, but the collector
does scan live regions as roots, so pointers from a region into the ordinary heap stay valid.

```
region scratch {
    let xs = alloc_in(i64, 1000, scratch);
    for i in 0..xs.len { xs[i] = i * i; }
    total += xs[999];                       // numbers copied out are fine
}                                           // xs is gone; one free for everything
```

The compiler tracks which region every pointer, slice and struct came from and rejects what would outlive it: returning it, assigning
it to a variable or field declared outside the block (or in an outer region), `free`-ing it, or storing it through a pointer of unknown origin.
It is an intra-function check: a callee that you pass a region pointer to could still stash it somewhere. Freed region memory is overwritten
with `0xDD` (unless `--unchecked`) so a stale use is loud. Not available with `--no-stdruntime`.

**Safety rules (checked at compile time)**
- Conditions must be `bool`. Variables are initialised and immutable unless `let mut`.
- Every non-void function must return on all paths; `match` must be exhaustive.
- Error unions must be handled (`try`/`catch`); an ignored error is a compile error.
- Pointers come from `&`, from C, or from a typed `null`; dereferencing null panics. Out-of-range literals, constant out-of-bounds indices and division by a literal 0 are errors.
- Warnings: unused variables/imports, unreachable code.

**Runtime checks (panic with `file:line:col`, exit code 101):** division by zero, array/slice/string bounds, slice ranges, shift
amounts, null dereference, narrowing conversions, allocation failure, and (with `--overflow-panic`) integer overflow.
`--unchecked` removes all of them.

**Freestanding builds (`--no-stdruntime`).** Only the integer/bounds helpers are emitted: no libc headers, no `print`, `String`,
`alloc`, GC, regions or `std.*` imports (except `std.mem`). A panic becomes a trap. Use it with `--entry=` and `export fn` to produce an
object file with no libc symbols (`nm -u` shows none), or call C yourself through `extern fn`.

**Modules:** `import a.b;` loads `a/b.cb` (relative to the entry file's directory, or `std/` for `std.*`);
the name is the last segment (or `as alias`). Only `pub` items are visible to importers; cycles are
rejected. All modules are emitted into one C translation unit, so cross-module calls inline.
Standard library (`std/`, written in Crumbs): `math`, `conv`, `str` (trim, find/rfind, split, join, replace, case conversion, compare, ...), `rand`, `list`, `mem`, `io`, and

- **`std.time`**: `monotonic_ns()`, `unix()`, `unix_ms()`, `sleep_ms(n)`; `Duration` (`from_ms`, `as_secs_f64`, `to_string()` -> `"1.500s"`, ...), `Stopwatch` (`start`, `elapsed`, `lap`, `reset`),
  and calendar dates: `from_unix(secs)`, `now_utc()`, `now_local()`, `DateTime.new(y, m, d, h, mi, s)`, `.format()`, `.iso()`, `.weekday_name()`, `.add_days(n)`, `is_leap_year`, `days_in_month`.
- **`std.fs`**: `read_to_string`, `read_lines`, `write`, `append`, `exists`, `is_file`, `is_dir`, `size`, `remove`, `make_dir` (errors `NotFound`, `IoError`).
- **`std.env`**: `args()`, `arg(i)`, `arg_count()`, `get(name)` (`NotSet`), `has(name)`.

## Compiler layout

| file | role |
|---|---|
| `crumbsc/lexer.py` | tokens, nested comments, escapes, number forms/suffixes, error recovery |
| `crumbsc/parser.py` | recursive descent + panic-mode recovery (reports many syntax errors per run) |
| `crumbsc/loader.py` | import resolution, cycle detection, dependency order |
| `crumbsc/checker.py` | name resolution, types, mutability, const-eval, control-flow analysis |
| `crumbsc/codegen.py` | C emission |
| `crumbsc/runtime.c` | the C runtime (arithmetic, strings, collector, regions, time/fs/env helpers), embedded into every output by `runtime.py` |
| `crumbsc/timing.py` | phase timing behind `-v` |
| `cookie.py` | the build tool (projects, profiles, option forwarding) |
| `crumbsc/diag.py` | rustc-style diagnostics with "did you mean" suggestions |
| `driver.py` | build pipeline, link plan (`link`/`--link`/`--pkg`), command line |

## Known limits (it is a toy)
No generic enums, trait bounds or closures; no `if` expressions; the collector is conservative and single-threaded. Slices are mutable views even of
immutable arrays. The generated C needs GCC/Clang (statement expressions, overflow builtins).
