<p align="center">
  <img src="docs/resources/crumbs-big.png" alt="Crumbs programming language logo" width="220" />
</p>

<p align="center">
  <img src="https://img.shields.io/badge/6%2C000%2B-Lines%20of%20code-white?style=flat&labelColor=black&logo=github" alt="6,000+ lines of code" />
  <img src="https://img.shields.io/badge/Written%20in-Python-yellow?style=flat&labelColor=blue&logo=python&logoColor=white&logoSize=auto" alt="Written in Python" />
  <a href="https://h1387lmao.github.io/Crumbs">
    <img src="https://img.shields.io/badge/View-Documentation-e6820e?style=for-the-badge&labelColor=white&logo=html5" alt="Badge">
  </a>
</p>

A small, statically typed, C-style toy programming language that compiles to C.

Crumbs is implemented in Python, with **8,000+ lines of code**, including approximately
**6,500+ lines of Python**. It compiles to a single C source file, which is then built
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
Bodies are checked per instantiation (errors carry a note saying which types were used). Without a bound, `a > b` simply must work
for the chosen `T`; to state what a type parameter must support, give it a trait bound (see **Traits**). Write `0 as T` for numeric zero.
`std.list` (growable `List<T>`) and `std.mem` (`swap`, `min`, `max`, `sort`) are generic.

**Traits.** A trait lists methods a type must have; `impl Trait for Type` supplies them. The compiler checks that every required method
is present, that none is extra, and that each signature matches the declaration. Bounds (`T: A + B`) go on generic functions, structs and
`impl` blocks and are checked at every instantiation, inferred or explicit. A method with a body is a default that an `impl` may keep or
override (`Self` is the implementing type); `trait Solid: Shape` requires every `Solid` type to be a `Shape` too. Dispatch is static
(monomorphized): there are no trait objects, and a trait is a bound, not a type.

```
trait Shape {
    fn area(self) -> f64;
    fn name(self) -> str;
    fn describe(self) -> String {                    // default method
        return string_from(self.name()) + " with area " + to_str(self.area());
    }
}
trait Solid: Shape { fn height(self) -> f64; }       // supertrait

struct Rect { w: f64, h: f64 }
impl Shape for Rect {
    fn area(self) -> f64 { return self.w * self.h; }
    fn name(self) -> str { return "rect"; }
}

fn total_area<T: Shape>(items: []T) -> f64 {         // bound, checked for every T
    let mut t = 0.0;
    for it in items { t += it.area(); }
    return t;
}
struct Labelled<T: Shape> { item: T, tag: str }
impl<T: Shape> Shape for Labelled<T> { ... }         // implemented only when T is a Shape
```
`total_area(dots)` with a type that has no `impl Shape` is an error: `the trait bound `Dot: Shape` is not satisfied`, with a note naming the
bound that required it. Traits and their impls can live in different modules (`impl lib.Greeter for Robot`).

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
**String interpolation.** `f"text {expr} more"` embeds any expression; `{expr:spec}` formats it, `{{` / `}}` are literal braces, and the
result is a `str`. The spec is `[[fill]align][sign][#][0][width][,|_][.precision][type]`:

```
let n = 255;  let pi = 3.14159;  let name = "world";
println(f"hello {name}! {1 + 2}");                      // hello world! 3
println(f"{n:x} {n:#b} {n:08.3f} {n:+} {1234567:,}");   // ff 0b11111111 0255.000 +255 1,234,567
println(f"[{name:>8}] [{name:*^9}] [{name:.3}] {pi:.2}"); // [   world] [**world**] [wor] 3.14
```

align `<` `>` `^`; sign `+` or space; `#` adds `0x`/`0b`/`0o`; `0` zero-pads numbers; `,` `_` group digits; `.N` is decimals for floats
and a character limit for text; types `d x X b o c` (integers), `f F e E g G` (floats), `?` (automatic representation). A bad spec is a
compile error pointing at it.

**Showing your own types (`Display`).** Implement the prelude trait `Display` (no import) and `println(x)`, `to_str(x)` and `f"{x}"` use
your text; the spec's width/alignment apply to it. `{x:?}` prints an automatic representation (`Point { x: 3, y: -4 }`) for any struct,
enum, array or slice of printable things. A struct with neither is a compile error that says what to add. `T: Display` is a valid bound;
numeric types and `bool` implement it, and `Duration` / `DateTime` already do.

```
struct Point { x: i32, y: i32 }
impl Display for Point {
    fn to_string(self) -> String { return string_from(f"({self.x}, {self.y})"); }
}
println(Point { x: 3, y: -4 });          // (3, -4)
println(f"{p:>10} {p:?}");               // "    (3, -4) Point { x: 3, y: -4 }"
```

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

**Garbage collector (on by default; `--no-gc` for plain malloc).** A conservative, **three-generation** collector (Linux; glibc or musl)
manages `alloc()`, `String` buffers and the string arena. New blocks start in generation 0 (the nursery). When the nursery has grown by
1 MiB the allocator runs one of three collections:

| collection | collects | survivors go to | when |
|---|---|---|---|
| minor | generation 0 | generation 1 | every nursery fill |
| mid | generations 0 and 1 | generation 2 (old) | every 8 minors, or generation 1 holds 4 MiB, or many cards are dirty |
| major | everything | generation 2 | the old generation grew by as much as it held (at least 16 MiB), or `gc_collect()` |

Within a collection blocks are white (not reached), grey (reached, contents not scanned) or black (reached and scanned); only the blocks
being collected are painted and indexed, so a minor collection costs time proportional to the young data, not the heap. Roots are the
stacks and registers of every thread, the data segment, live regions, each thread's string arena, and the **old-to-young pointers**.
Those are found with a **write barrier**: the compiler emits `cb_wb_range` after each store of a pointer-holding value through a pointer,
slice, field or `write_at`, which dirties a 512-byte card; minor and mid collections scan the dirty cards as roots. Marking is
stop-the-world; young generations are swept immediately and the old generation lazily (a few blocks per allocation). Interior pointers keep
a block alive, and blocks known to hold no pointers (numbers, bytes) are never scanned.

**Collector modes (`--gc-mode`, cookie: `gc_mode` / `gc_interval`).**

| mode | who decides when to collect | for |
|---|---|---|
| `auto` (default) | the allocator, when the nursery fills | everyone; nothing to configure |
| `thread` | a background collector thread, every `--gc-interval` ms (default 100) | beginners who want it hands-off: allocating code never stops to collect |
| `manual` | you, with `gc_collect()` / `gc_collect_gen(g)` | advanced users with frame loops or latency budgets |

In `thread` mode the compiler adds a cheap safepoint check at function entry and in every loop iteration so the collector can stop the
program promptly (allocations, sleeps and blocking input are safepoints too). `manual` never collects by itself, so memory grows until you
collect. `gc_collect_gen` is available only in `manual` mode (elsewhere it is a compile error pointing at `--gc-mode=manual`).

Controls: `gc_collect()` forces a major collection in every mode, `gc_collect_gen(g)` (manual mode) collects generations `0..g`,
`gc_bytes()` reports bytes in use, `gc_collections()` counts all cycles, and `gc_gen_bytes(g)` / `gc_gen_collections(g)` report one
generation (all no-ops / 0 with `--no-gc`).
Compile with `--cflag=-DCB_GC_VERIFY` to check every collection against a full trace of the heap (it aborts on a missed barrier or a
grey block left over); the thresholds can be tuned with `-DCB_GC_NURSERY=`, `CB_GC_MID`, `CB_GC_MID_EVERY`, `CB_GC_MAJOR_MIN` and
`CB_GC_CARD_BUDGET`. `free()` and destructors stay legal and release memory immediately. Caveats: memory given to C and kept only on the C
side is not seen (keep a Crumbs reference to it); C code that stores Crumbs pointers into Crumbs memory should call
`cb_gc_write_barrier(p, size)` afterwards; under AddressSanitizer use `ASAN_OPTIONS=detect_stack_use_after_return=0`.
With threads (`std.thread`) the heap is locked and a collection waits (up to 100 ms) for the other threads to reach an allocation or a
blocking call; a thread that is busy computing only postpones the collection.

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
rejected. All modules are emitted into one C translation unit, so cross-module calls inline. Traits, like every other `pub` item, can be declared in one module and implemented in another.

## Standard library

Written in Crumbs and imported with `import std.name;` (`String`, `str`, `List<T>` and the `Display` trait need no import). Related modules are grouped:

| area | modules | what they give you |
|---|---|---|
| input / output | `std.io` | `getc` (`io.Eof`), `putc`, `get_line`, `flush` |
| collections | `std.list`, `std.mem` | growable `List<T>` for every element type (`new`, `with_capacity`, `from_slice`, `push`, `pop`, `at`, `get`, `set`, `insert`, `remove`, `swap_remove`, `extend`, `view`, `clear`, `release`, `reverse`, `sort_by`, `for_each`, `find_by`); `swap`, `min`, `max`, `sort` |
| text, numbers | `std.str`, `std.conv`, `std.math`, `std.rand` | trim, find/rfind, split, join, replace, case conversion, compare; text/number conversion; math; random numbers |
| time | `std.time` | `monotonic_ns`, `unix`, `unix_ms`, `sleep_ms`; `Duration` (`from_ms`, `as_secs_f64`, `to_string()`), `Stopwatch` (`start`, `elapsed`, `lap`, `reset`); calendar dates: `from_unix`, `now_utc`, `now_local`, `DateTime.new`, `.format()`, `.iso()`, `.weekday_name()`, `.add_days(n)`, `is_leap_year`, `days_in_month` |
| files, environment | `std.fs`, `std.env` | `read_to_string`, `read_lines`, `write`, `append`, `exists`, `is_file`, `is_dir`, `size`, `remove`, `make_dir` (errors `NotFound`, `IoError`); `args`, `arg`, `arg_count`, `get` (`NotSet`), `has` |
| threads | `std.thread` | `spawn(f, arg)` / `spawn_fn(f)` -> `Thread` (`.join()`), `Mutex`, `Condvar`, `AtomicI64`, generic `channel<T>()` (`send`, `recv`, `close`; `Closed`), `sleep_ms`, `yield_now`, `current_id`, `cpu_count` |

`std.list` owns its elements and releases them (running destructors and freeing `String`s) automatically; `list.List<T>` works for numbers,
`str`, `String`, structs, arrays, function pointers and nested lists. `std.time`, `std.fs`, `std.env`, `std.io` and `std.thread` contain no C
helpers: they bind libc and pthread with plain `extern fn` / `extern struct` / `extern const` declarations, exactly like user code does.
(The only runtime hooks they call are the collector's thread notifications in `std.thread`.)

## Compiler layout

| file | role |
|---|---|
| `crumbsc/lexer.py` | tokens, nested comments, escapes, number forms/suffixes, error recovery |
| `crumbsc/parser.py` | recursive descent + panic-mode recovery (reports many syntax errors per run) |
| `crumbsc/loader.py` | import resolution, cycle detection, dependency order |
| `crumbsc/checker.py` | name resolution, types, mutability, const-eval, control-flow analysis, trait conformance and bound checking |
| `crumbsc/codegen.py` | C emission |
| `crumbsc/runtime.c` | the C runtime (arithmetic, strings, the generational collector and write barrier, regions), embedded into every output by `runtime.py` |
| `crumbsc/timing.py` | phase timing behind `-v` |
| `cookie.py` | the build tool (projects, profiles, option forwarding) |
| `crumbsc/diag.py` | rustc-style diagnostics with "did you mean" suggestions |
| `driver.py` | build pipeline, link plan (`link`/`--link`/`--pkg`), command line |

## Preprocessor

Source text goes through a preprocessor before lexing. Directives start with `#` at the beginning of a line:

```
#define LIMIT 100
#define SQUARE(x) ((x) * (x))
#define LOG(fmt, ...) println(fmt __VA_OPT__(,) __VA_ARGS__)   // variadic; #x makes a string, a##b pastes
#ifdef CB_OS_LINUX            // #if / #ifdef / #ifndef / #elif / #else / #endif, nestable
#if LIMIT > 50 && defined(FEATURE) && !CB_GC
...
#endif
#endif
#error / #warning message     // reported as compiler diagnostics
```

`#if` expressions are integers with `defined(X)`, `! ~ - + * / % << >> < <= > >= == != & ^ | && ||` and `?:`; unknown names are 0.
Strings, char literals and comments are never touched, and line numbers never change (directive and skipped lines become empty), so
error messages still point at the right line; a line altered by macros gets a note showing the original. Predefined: `__FILE__`,
`__LINE__`, `__COUNTER__`, `__CRUMBS__`, `CB_OS_LINUX/MACOS/WINDOWS`, `CB_ARCH_X86_64/AARCH64`, `CB_GC`, `CB_NO_STDRUNTIME`, `CB_OVERFLOW_PANIC`.
`-DNAME[=VALUE]` / `-UNAME` work on `crumbs` and `cookie` (`defines = [...]` under `[build]`), and `crumbs pp file.cb` prints the result.
Not supported: `#include` (use `import`, or `include "x.h";` for C headers). Macros are per file, and a call must fit on one line.

## Known limits (it is a toy)
No generic enums, trait objects, generic traits or closures; no `if` expressions; the collector is conservative and stop-the-world (generational, so most pauses are short). Slices are mutable views even of
immutable arrays. Interpolated strings must fit on one line, and `Display` is static (no `dyn Display`). The generated C needs GCC/Clang (statement expressions, overflow builtins).
