/* ---- Crumbs runtime ---------------------------------------------------- */
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#ifndef CB_NOSTD            /* --no-stdruntime: only the freestanding headers above are used */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#ifdef CB_GC
#include <setjmp.h>
#endif
#endif

#if defined(__GNUC__) || defined(__clang__)
#  define CB_LIKELY(x)   __builtin_expect(!!(x), 1)
#  define CB_UNLIKELY(x) __builtin_expect(!!(x), 0)
#  define CB_COLD        __attribute__((cold, noreturn, noinline))
#  define CB_UNUSED      __attribute__((unused))
#else
#  error "Crumbs's generated C needs GCC or Clang (statement expressions, overflow builtins)"
#endif

typedef struct { const uint8_t *ptr; int64_t len; } cb_str;

#ifdef CB_NOSTD
/* no libc: a panic just traps (there is nothing to print with) */
CB_COLD static void cb_panic(const char *loc, const char *msg) { (void)loc; (void)msg; __builtin_trap(); }
CB_COLD static void cb_panic_str(const char *loc, cb_str m) { (void)loc; (void)m; __builtin_trap(); }
CB_COLD static void cb_panic_idx(const char *loc, int64_t i, int64_t n) { (void)loc; (void)i; (void)n; __builtin_trap(); }
CB_COLD static void cb_panic_rng(const char *loc, int64_t lo, int64_t hi, int64_t n) { (void)loc; (void)lo; (void)hi; (void)n; __builtin_trap(); }
#else
CB_COLD static void cb_panic(const char *loc, const char *msg) {
    fflush(stdout);
    fprintf(stderr, "panic: %s\n  at %s\n", msg, loc);
    exit(101);
}
CB_COLD static void cb_panic_str(const char *loc, cb_str m) {
    fflush(stdout);
    fprintf(stderr, "panic: %.*s\n  at %s\n", (int)m.len, (const char *)m.ptr, loc);
    exit(101);
}
CB_COLD static void cb_panic_idx(const char *loc, int64_t i, int64_t n) {
    fflush(stdout);
    fprintf(stderr, "panic: index out of bounds: the length is %lld but the index is %lld\n  at %s\n",
            (long long)n, (long long)i, loc);
    exit(101);
}
CB_COLD static void cb_panic_rng(const char *loc, int64_t lo, int64_t hi, int64_t n) {
    fflush(stdout);
    if (lo > hi)
        fprintf(stderr, "panic: slice start %lld is greater than end %lld\n  at %s\n", (long long)lo, (long long)hi, loc);
    else
        fprintf(stderr, "panic: slice range %lld..%lld is out of bounds for length %lld\n  at %s\n",
                (long long)lo, (long long)hi, (long long)n, loc);
    exit(101);
}

#endif

#ifdef CB_UNCHECKED
static inline int64_t cb_idx(int64_t i, int64_t n, const char *loc) { (void)n; (void)loc; return i; }
static inline void cb_chk_rng(int64_t lo, int64_t hi, int64_t n, const char *loc) { (void)lo; (void)hi; (void)n; (void)loc; }
#else
static inline int64_t cb_idx(int64_t i, int64_t n, const char *loc) {
    if (CB_UNLIKELY((uint64_t)i >= (uint64_t)n)) cb_panic_idx(loc, i, n);
    return i;
}
static inline void cb_chk_rng(int64_t lo, int64_t hi, int64_t n, const char *loc) {
    if (CB_UNLIKELY(lo < 0 || hi < lo || hi > n)) cb_panic_rng(loc, lo, hi, n);
}
#endif

/* integer arithmetic: wraps (two's complement) by default; --overflow-panic makes overflow a panic */
#if defined(CB_OVERFLOW_PANIC) && !defined(CB_UNCHECKED)
#define CB_OVF 1
#define CB_CHK_ADD(T, UT) if (CB_UNLIKELY(__builtin_add_overflow(a, b, &r))) cb_panic(l, "integer overflow in addition");
#define CB_CHK_SUB(T, UT) if (CB_UNLIKELY(__builtin_sub_overflow(a, b, &r))) cb_panic(l, "integer overflow in subtraction");
#define CB_CHK_MUL(T, UT) if (CB_UNLIKELY(__builtin_mul_overflow(a, b, &r))) cb_panic(l, "integer overflow in multiplication");
#else
#define CB_OVF 0
#define CB_CHK_ADD(T, UT) r = (T)(UT)((uint64_t)(UT)a + (uint64_t)(UT)b);
#define CB_CHK_SUB(T, UT) r = (T)(UT)((uint64_t)(UT)a - (uint64_t)(UT)b);
#define CB_CHK_MUL(T, UT) r = (T)(UT)((uint64_t)(UT)a * (uint64_t)(UT)b);
#endif

#define CB_INT_COMMON(N, T, UT, BITS) \
static inline T cb_add_##N(T a, T b, const char *l) { T r; CB_CHK_ADD(T, UT) return r; } \
static inline T cb_sub_##N(T a, T b, const char *l) { T r; CB_CHK_SUB(T, UT) return r; } \
static inline T cb_mul_##N(T a, T b, const char *l) { T r; CB_CHK_MUL(T, UT) return r; } \
static inline T cb_shl_##N(T a, int64_t n, const char *l) { \
    if (CB_UNLIKELY((uint64_t)n >= (uint64_t)(BITS))) cb_panic(l, "shift amount out of range"); \
    return (T)(UT)((uint64_t)(UT)a << n); } \
static inline T cb_shr_##N(T a, int64_t n, const char *l) { \
    if (CB_UNLIKELY((uint64_t)n >= (uint64_t)(BITS))) cb_panic(l, "shift amount out of range"); \
    return (T)(a >> n); }

#ifdef CB_UNCHECKED
#define CB_DIVCHK_Z(l)
#else
#define CB_DIVCHK_Z(l) if (CB_UNLIKELY(b == 0)) cb_panic(l, "division by zero");
#endif

#define CB_INT_S(N, T, UT, BITS, MINV) \
CB_INT_COMMON(N, T, UT, BITS) \
static inline T cb_div_##N(T a, T b, const char *l) { \
    CB_DIVCHK_Z(l) \
    if (CB_UNLIKELY(b == (T)-1)) { \
        if (CB_OVF && a == (MINV)) cb_panic(l, "integer overflow in division"); \
        return (T)(UT)(0 - (uint64_t)(UT)a); } \
    return (T)(a / b); } \
static inline T cb_rem_##N(T a, T b, const char *l) { \
    CB_DIVCHK_Z(l) \
    if (CB_UNLIKELY(b == (T)-1)) { \
        if (CB_OVF && a == (MINV)) cb_panic(l, "integer overflow in remainder"); \
        return 0; } \
    return (T)(a % b); } \
static inline T cb_neg_##N(T a, const char *l) { \
    if (CB_OVF && CB_UNLIKELY(a == (MINV))) cb_panic(l, "integer overflow in negation"); \
    return (T)(UT)(0 - (uint64_t)(UT)a); }

#define CB_INT_U(N, T, UT, BITS) \
CB_INT_COMMON(N, T, UT, BITS) \
static inline T cb_div_##N(T a, T b, const char *l) { CB_DIVCHK_Z(l) return (T)(a / b); } \
static inline T cb_rem_##N(T a, T b, const char *l) { CB_DIVCHK_Z(l) return (T)(a % b); }

CB_INT_S(i8,  int8_t,  uint8_t,  8,  INT8_MIN)
CB_INT_S(i16, int16_t, uint16_t, 16, INT16_MIN)
CB_INT_S(i32, int32_t, uint32_t, 32, INT32_MIN)
CB_INT_S(i64, int64_t, uint64_t, 64, INT64_MIN)
CB_INT_U(u8,  uint8_t,  uint8_t,  8)
CB_INT_U(u16, uint16_t, uint16_t, 16)
CB_INT_U(u32, uint32_t, uint32_t, 32)
CB_INT_U(u64, uint64_t, uint64_t, 64)

/* range-checked implicit integer conversions (via a 128-bit intermediate) */
#ifdef CB_UNCHECKED
#define CB_CVT(N, T, MINV, MAXV) \
static inline T cb_cvt_##N(__int128 v, const char *l) { (void)l; return (T)v; }
#else
#define CB_CVT(N, T, MINV, MAXV) \
CB_COLD static void cb_panic_cvt_##N(const char *l) { \
    cb_panic(l, "value out of range for `" #N "` in implicit conversion (use `as` to truncate)"); } \
static inline T cb_cvt_##N(__int128 v, const char *l) { \
    if (CB_UNLIKELY(v < (__int128)(MINV) || v > (__int128)(MAXV))) cb_panic_cvt_##N(l); \
    return (T)v; }
#endif
CB_CVT(i8,  int8_t,  INT8_MIN,  INT8_MAX)
CB_CVT(i16, int16_t, INT16_MIN, INT16_MAX)
CB_CVT(i32, int32_t, INT32_MIN, INT32_MAX)
CB_CVT(i64, int64_t, INT64_MIN, INT64_MAX)
CB_CVT(u8,  uint8_t,  0, UINT8_MAX)
CB_CVT(u16, uint16_t, 0, UINT16_MAX)
CB_CVT(u32, uint32_t, 0, UINT32_MAX)
CB_CVT(u64, uint64_t, 0, UINT64_MAX)

/* saturating float -> int casts (NaN -> 0) */
#define CB_F2I(N, T, MINV, MAXV) \
static inline T cb_f2i_##N(double x) { \
    if (x != x) return 0; \
    if (x <= (double)(MINV)) return (MINV); \
    if (x >= (double)(MAXV)) return (MAXV); \
    return (T)x; }
CB_F2I(i8,  int8_t,  INT8_MIN,  INT8_MAX)
CB_F2I(i16, int16_t, INT16_MIN, INT16_MAX)
CB_F2I(i32, int32_t, INT32_MIN, INT32_MAX)
CB_F2I(i64, int64_t, INT64_MIN, INT64_MAX)
CB_F2I(u8,  uint8_t,  0, UINT8_MAX)
CB_F2I(u16, uint16_t, 0, UINT16_MAX)
CB_F2I(u32, uint32_t, 0, UINT32_MAX)
CB_F2I(u64, uint64_t, 0, UINT64_MAX)

static inline bool cb_str_eq(cb_str a, cb_str b) {
    return a.len == b.len && (a.len == 0 || __builtin_memcmp(a.ptr, b.ptr, (size_t)a.len) == 0);
}

#ifndef CB_NOSTD
/* ---- memory: plain malloc, or a conservative mark-and-sweep collector (--gc) ----------------
   cb_mem_alloc(n, atomic)  zeroed memory; `atomic` = contains no pointers, so the collector never scans it
   cb_mem_realloc / cb_mem_free  explicit release is always allowed (and optional under --gc)       */
/* ---- regions: scoped bump allocators (`region r { ... alloc_in(T, n, r) ... }`) ----------------
   A region hands out zeroed memory from big chunks and gives all of it back when its block ends,
   on every way out (end of block, break, continue, return). Nothing inside is traced or swept
   individually; when the collector runs it scans the live regions' memory as roots, so pointers from a
   region into the ordinary heap keep their targets alive. Freed memory is poisoned unless --unchecked. */
typedef struct cb_RegionChunk { struct cb_RegionChunk *next; size_t cap, used; } cb_RegionChunk;
typedef struct cb_Region { cb_RegionChunk *chunks; struct cb_Region *prev, *next; } cb_Region;
static cb_Region *cb_regions;
#define CB_REGION_CHUNK ((size_t)64 << 10)
static cb_Region *cb_region_new(void) {
    cb_Region *r = (cb_Region *)calloc(1, sizeof *r);
    if (!r) { fputs("panic: out of memory (region)\n", stderr); exit(101); }
    r->next = cb_regions;
    if (cb_regions) cb_regions->prev = r;
    cb_regions = r;
    return r;
}
static void *cb_region_alloc(cb_Region *r, int64_t n, int64_t size, const char *l) {
    if (CB_UNLIKELY(n < 0)) cb_panic(l, "negative allocation size");
    size_t total;
    if (CB_UNLIKELY(__builtin_mul_overflow((size_t)n, (size_t)size, &total))) cb_panic(l, "allocation size overflow");
    total = (total + 15) & ~(size_t)15;
    if (total == 0) total = 16;
    cb_RegionChunk *c = r->chunks;
    if (!c || c->cap - c->used < total) {
        size_t cap = total > CB_REGION_CHUNK ? total : CB_REGION_CHUNK;
        c = (cb_RegionChunk *)calloc(1, sizeof *c + 16 + cap);
        if (!c) cb_panic(l, "out of memory");
        c->cap = cap;
        c->next = r->chunks;
        r->chunks = c;
    }
    /* data starts at a 16-byte boundary after the header */
    uint8_t *base = (uint8_t *)(((uintptr_t)(c + 1) + 15) & ~(uintptr_t)15);
    void *p = base + c->used;
    c->used += total;
    return p;
}
static void cb_region_free(cb_Region *r) {
    for (cb_RegionChunk *c = r->chunks, *nx; c; c = nx) {
        nx = c->next;
#ifndef CB_UNCHECKED
        memset((void *)(c + 1), 0xDD, c->cap);        /* a pointer that outlived its region reads 0xDD.. instead of old data */
#endif
        free(c);
    }
    if (r->prev) r->prev->next = r->next; else cb_regions = r->next;
    if (r->next) r->next->prev = r->prev;
    free(r);
}
#ifdef CB_GC
static void cb_gc_scan(const void *lo, const void *hi);
static void cb_region_roots(void) {
    for (cb_Region *r = cb_regions; r; r = r->next)
        for (cb_RegionChunk *c = r->chunks; c; c = c->next)
            cb_gc_scan((const void *)(c + 1), (const uint8_t *)(c + 1) + 16 + c->used);
}
#endif

#ifndef CB_GC
static void *cb_mem_alloc(size_t n, int atomic) { (void)atomic; return calloc(n ? n : 1, 1); }
static void *cb_mem_realloc(void *p, size_t old, size_t n) { (void)old; return realloc(p, n); }
static void cb_mem_free(void *p) { free(p); }
static void cb_gc_collect(void) {}
static int64_t cb_gc_bytes(void) { return 0; }
static int64_t cb_gc_collections(void) { return 0; }
#define CB_GC_INIT() ((void)0)
#else
/* Single-threaded, Linux/glibc or musl. Every block has a 32-byte header in a doubly linked list.
   Roots: the machine stack (and registers), and the data/bss segments. Interior pointers keep a block alive.
   Memory handed to C and kept only there is NOT seen by the collector: keep a Crumbs reference to it. */
typedef struct cb_hdr { size_t n; struct cb_hdr *prev, *next; uintptr_t flags; } cb_hdr;   /* flags: 1 mark, 2 atomic */
extern char __data_start[] __attribute__((weak));
extern char _end[] __attribute__((weak));
static cb_hdr *cb_gc_all;
static size_t cb_gc_live, cb_gc_since, cb_gc_next = (size_t)8 << 20, cb_gc_runs;
static void *cb_gc_stack_bottom;
static cb_hdr **cb_gc_idx; static size_t cb_gc_nidx;
static cb_hdr **cb_gc_work; static size_t cb_gc_nwork, cb_gc_capwork;
#define CB_GC_INIT() (cb_gc_stack_bottom = __builtin_frame_address(0))

static int cb_gc_cmp(const void *a, const void *b) {
    uintptr_t x = (uintptr_t)*(cb_hdr *const *)a, y = (uintptr_t)*(cb_hdr *const *)b;
    return x < y ? -1 : x > y;
}
static void cb_gc_mark_word(uintptr_t w) {
    size_t lo = 0, hi = cb_gc_nidx;
    while (lo < hi) {                       /* last block whose data start is <= w */
        size_t mid = (lo + hi) / 2;
        if ((uintptr_t)(cb_gc_idx[mid] + 1) <= w) lo = mid + 1; else hi = mid;
    }
    if (!lo) return;
    cb_hdr *h = cb_gc_idx[lo - 1];
    if (w >= (uintptr_t)(h + 1) + (h->n ? h->n : 1) || (h->flags & 1)) return;
    h->flags |= 1;
    if (h->flags & 2) return;               /* atomic: nothing inside to follow */
    if (cb_gc_nwork == cb_gc_capwork) {
        cb_gc_capwork = cb_gc_capwork ? cb_gc_capwork * 2 : 1024;
        cb_gc_work = (cb_hdr **)realloc(cb_gc_work, cb_gc_capwork * sizeof *cb_gc_work);
        if (!cb_gc_work) { fputs("panic: out of memory (gc)\n", stderr); exit(101); }
    }
    cb_gc_work[cb_gc_nwork++] = h;
}
__attribute__((no_sanitize("address"))) static void cb_gc_scan(const void *lo, const void *hi) {
    uintptr_t a = ((uintptr_t)lo + sizeof(void *) - 1) & ~(uintptr_t)(sizeof(void *) - 1);
    for (; a + sizeof(void *) <= (uintptr_t)hi; a += sizeof(void *)) cb_gc_mark_word(*(const uintptr_t *)a);
}
__attribute__((noinline, no_sanitize("address"))) static void cb_gc_collect(void) {
    size_t n = 0;
    for (cb_hdr *h = cb_gc_all; h; h = h->next) n++;
    cb_gc_idx = (cb_hdr **)malloc((n ? n : 1) * sizeof *cb_gc_idx);
    if (!cb_gc_idx) return;
    cb_gc_nidx = 0;
    for (cb_hdr *h = cb_gc_all; h; h = h->next) { h->flags &= ~(uintptr_t)1; cb_gc_idx[cb_gc_nidx++] = h; }
    qsort(cb_gc_idx, cb_gc_nidx, sizeof *cb_gc_idx, cb_gc_cmp);
    jmp_buf regs;                            /* spill callee-saved registers onto the stack */
    setjmp(regs);
    void *sp = (void *)&regs;
    if (cb_gc_stack_bottom) cb_gc_scan(sp < cb_gc_stack_bottom ? sp : cb_gc_stack_bottom,
                                       sp < cb_gc_stack_bottom ? cb_gc_stack_bottom : sp);
    if (__data_start && _end) cb_gc_scan(__data_start, _end);
    cb_region_roots();                       /* pointers stored inside live regions keep their targets alive */
    while (cb_gc_nwork) {
        cb_hdr *h = cb_gc_work[--cb_gc_nwork];
        cb_gc_scan(h + 1, (uint8_t *)(h + 1) + h->n);
    }
    size_t live = 0;
    for (cb_hdr *h = cb_gc_all, *nx; h; h = nx) {
        nx = h->next;
        if (h->flags & 1) { live += h->n; continue; }
        if (h->prev) h->prev->next = h->next; else cb_gc_all = h->next;
        if (h->next) h->next->prev = h->prev;
        free(h);
    }
    free(cb_gc_idx); cb_gc_idx = 0; cb_gc_nidx = 0;
    cb_gc_live = live;
    cb_gc_since = 0;
    cb_gc_runs++;
    cb_gc_next = live > ((size_t)4 << 20) ? live : ((size_t)4 << 20);   /* collect again after allocating this much more */
}
static void *cb_mem_alloc(size_t n, int atomic) {
    if (!cb_gc_stack_bottom) {              /* library mode: no main() ran CB_GC_INIT; approximate from here */
        extern void *__libc_stack_end __attribute__((weak));
        cb_gc_stack_bottom = &__libc_stack_end ? __libc_stack_end : __builtin_frame_address(0);
    }
    if (cb_gc_since >= cb_gc_next) cb_gc_collect();
    cb_hdr *h = (cb_hdr *)calloc(1, sizeof(cb_hdr) + (n ? n : 1));
    if (!h) { cb_gc_collect(); h = (cb_hdr *)calloc(1, sizeof(cb_hdr) + (n ? n : 1)); }
    if (!h) return 0;
    h->n = n; h->flags = atomic ? 2 : 0;
    h->next = cb_gc_all;
    if (cb_gc_all) cb_gc_all->prev = h;
    cb_gc_all = h;
    cb_gc_live += n; cb_gc_since += n;
    return h + 1;
}
static void cb_mem_free(void *p) {
    if (!p) return;
    cb_hdr *h = (cb_hdr *)p - 1;
    if (h->prev) h->prev->next = h->next; else cb_gc_all = h->next;
    if (h->next) h->next->prev = h->prev;
    cb_gc_live -= h->n;
    free(h);
}
static void *cb_mem_realloc(void *p, size_t old, size_t n) {
    if (!p) return cb_mem_alloc(n, 1);
    cb_hdr *h = (cb_hdr *)p - 1;
    void *q = cb_mem_alloc(n, (int)((h->flags & 2) != 0));
    if (!q) return 0;
    memcpy(q, p, old < n ? old : n);
    cb_mem_free(p);
    return q;
}
static int64_t cb_gc_bytes(void) { return (int64_t)cb_gc_live; }
static int64_t cb_gc_collections(void) { return (int64_t)cb_gc_runs; }
#endif

static inline void *cb_alloc(int64_t n, int64_t size, int atomic, const char *l) {
    if (CB_UNLIKELY(n < 0)) cb_panic(l, "negative allocation size");
    size_t total;
    if (CB_UNLIKELY(__builtin_mul_overflow((size_t)n, (size_t)size, &total))) cb_panic(l, "allocation size overflow");
    void *p = cb_mem_alloc(total, atomic);
    if (CB_UNLIKELY(!p)) cb_panic(l, "out of memory");
    return p;
}

/* printing */
static void cb_print_i(long long v) { printf("%lld", v); }
static void cb_print_u(unsigned long long v) { printf("%llu", v); }
static void cb_print_bool(bool v) { fputs(v ? "true" : "false", stdout); }
static void cb_print_str(cb_str s) { fwrite(s.ptr, 1, (size_t)s.len, stdout); }
static void cb_print_cstr(const char *s) { fputs(s, stdout); }
static void cb_print_ptr(const void *p) { printf("%p", p); }
static void cb_fmt_shortest(char *buf, size_t cap, double v, int is32) {
    int p, ex = 0;
    for (p = 1; p <= (is32 ? 9 : 17); p++) {
        snprintf(buf, cap, "%.*e", p - 1, v);
        if (is32 ? (strtof(buf, NULL) == (float)v) : (strtod(buf, NULL) == v)) break;
    }
    ex = atoi(strchr(buf, 'e') + 1);
    if (ex >= -5 && ex < 16) {
        int dec = p - 1 - ex;
        if (dec < 0) dec = 0;
        snprintf(buf, cap, "%.*f", dec, v);
        if (!strchr(buf, '.')) strcat(buf, ".0");
    } else {
        /* scientific: mantissa digits already minimal; normalise "1e+21" style */
        char *e = strchr(buf, 'e');
        char mant[32]; size_t ml = (size_t)(e - buf);
        memcpy(mant, buf, ml); mant[ml] = 0;
        snprintf(buf, cap, "%se%+03d", mant, ex);
    }
}
static void cb_print_f64(double v) {
    char buf[64];
    if (v != v) { fputs("nan", stdout); return; }
    if (isinf(v)) { fputs(v < 0 ? "-inf" : "inf", stdout); return; }
    cb_fmt_shortest(buf, sizeof buf, v, 0);
    fputs(buf, stdout);
}
static void cb_print_f32(float v) {
    char buf[64];
    if (v != v) { fputs("nan", stdout); return; }
    if (isinf(v)) { fputs(v < 0 ? "-inf" : "inf", stdout); return; }
    cb_fmt_shortest(buf, sizeof buf, (double)v, 1);
    fputs(buf, stdout);
}
/* ---- strings: bump arena + concatenation -------------------------------- */
static struct { uint8_t *top, *end; } cb_arena;

static uint8_t *cb_arena_alloc(size_t n, const char *loc) {
    if (CB_UNLIKELY((size_t)(cb_arena.end - cb_arena.top) < n)) {
        size_t cap = n > ((size_t)1 << 19) ? n * 2 : ((size_t)1 << 20);
        uint8_t *chunk = (uint8_t *)cb_mem_alloc(cap, 1);
        if (CB_UNLIKELY(!chunk)) cb_panic(loc, "out of memory");
        cb_arena.top = chunk;
        cb_arena.end = chunk + cap;
    }
    uint8_t *p = cb_arena.top;
    cb_arena.top += n;
    return p;
}

/* Concatenate n strings into one. If the first operand is the newest arena allocation
   (the `s += x` pattern) it is extended in place, making repeated appends amortised O(1).
   Bytes past a string's length are owned by nobody, so other views stay valid. */
static cb_str cb_str_catv(const cb_str *p, int n, const char *loc) {
    int64_t total = 0;
    for (int i = 0; i < n; i++) {
        if (CB_UNLIKELY(__builtin_add_overflow(total, p[i].len, &total))) cb_panic(loc, "string too long");
    }
    if (p[0].len > 0 && p[0].ptr + p[0].len == cb_arena.top &&
        (size_t)(cb_arena.end - cb_arena.top) >= (size_t)(total - p[0].len)) {
        uint8_t *w = cb_arena.top;
        for (int i = 1; i < n; i++) { if (p[i].len) memcpy(w, p[i].ptr, (size_t)p[i].len); w += p[i].len; }
        cb_arena.top = w;
        return (cb_str){p[0].ptr, total};
    }
    if (total == 0) return (cb_str){(const uint8_t *)"", 0};
    uint8_t *buf = cb_arena_alloc((size_t)total, loc);
    uint8_t *w = buf;
    for (int i = 0; i < n; i++) { if (p[i].len) memcpy(w, p[i].ptr, (size_t)p[i].len); w += p[i].len; }
    return (cb_str){buf, total};
}

static cb_str cb_str_copy_cstr(const char *s, const char *loc) {
    size_t n = strlen(s);
    uint8_t *buf = cb_arena_alloc(n ? n : 1, loc);
    memcpy(buf, s, n);
    return (cb_str){buf, (int64_t)n};
}
static cb_str cb_str_view_c(const char *s) { return (cb_str){(const uint8_t *)s, (int64_t)strlen(s)}; }
static cb_str cb_str_from_i(long long v, const char *loc) { char b[32]; snprintf(b, sizeof b, "%lld", v); return cb_str_copy_cstr(b, loc); }
static cb_str cb_str_from_u(unsigned long long v, const char *loc) { char b[32]; snprintf(b, sizeof b, "%llu", v); return cb_str_copy_cstr(b, loc); }
static cb_str cb_str_from_f(double v, int is32, const char *loc) {
    char b[64];
    if (v != v) return cb_str_view_c("nan");
    if (isinf(v)) return cb_str_view_c(v < 0 ? "-inf" : "inf");
    cb_fmt_shortest(b, sizeof b, v, is32);
    return cb_str_copy_cstr(b, loc);
}
/* ---- String: owned, heap-allocated, growable ---------------------------- */
typedef struct { uint8_t *ptr; int64_t len; int64_t cap; } cb_String;

static inline cb_str cb_string_view(cb_String s) {
    return (cb_str){ s.ptr ? (const uint8_t *)s.ptr : (const uint8_t *)"", s.len };
}
static cb_String cb_string_reserve(cb_String s, int64_t extra, const char *loc) {
    int64_t need;
    if (CB_UNLIKELY(__builtin_add_overflow(s.len, extra, &need))) cb_panic(loc, "string too long");
    if (need <= s.cap) return s;
    int64_t cap = s.cap * 2;
    if (cap < need) cap = need;
    if (cap < 16) cap = 16;
    uint8_t *p = (uint8_t *)cb_mem_realloc(s.ptr, (size_t)s.cap, (size_t)cap);
    if (CB_UNLIKELY(!p)) cb_panic(loc, "out of memory");
    s.ptr = p;
    s.cap = cap;
    return s;
}
static cb_String cb_string_append(cb_String s, cb_str v, const char *loc) {
    if (v.len == 0) return s;
    int64_t off = -1;   /* `s += s[1..]`: the source may live inside our own buffer, which realloc can move */
    if (s.ptr && v.ptr >= s.ptr && v.ptr < s.ptr + s.len) off = (int64_t)(v.ptr - s.ptr);
    s = cb_string_reserve(s, v.len, loc);
    const uint8_t *src = off >= 0 ? s.ptr + off : v.ptr;
    memmove(s.ptr + s.len, src, (size_t)v.len);
    s.len += v.len;
    return s;
}
static cb_String cb_string_push(cb_String s, uint8_t c, const char *loc) {
    s = cb_string_reserve(s, 1, loc);
    s.ptr[s.len++] = c;
    return s;
}
static cb_String cb_string_from(cb_str v, const char *loc) {
    cb_String s = {0, 0, 0};
    return cb_string_append(s, v, loc);
}
static cb_String cb_string_catv(const cb_str *p, int n, const char *loc) {
    int64_t total = 0;
    for (int i = 0; i < n; i++)
        if (CB_UNLIKELY(__builtin_add_overflow(total, p[i].len, &total))) cb_panic(loc, "string too long");
    cb_String s = {0, 0, 0};
    if (total == 0) return s;
    s = cb_string_reserve(s, total, loc);
    for (int i = 0; i < n; i++) {
        if (p[i].len) memcpy(s.ptr + s.len, p[i].ptr, (size_t)p[i].len);
        s.len += p[i].len;
    }
    return s;
}
static void cb_string_free(cb_String s) { cb_mem_free(s.ptr); }

#endif /* !CB_NOSTD */

/* ---- C interop ----------------------------------------------------------- */
#ifdef __APPLE__
#define CB_ASM(n) __asm__("_" n)
#else
#define CB_ASM(n) __asm__(n)
#endif
#ifndef CB_NOSTD
static uint8_t *cb_cstr(cb_str s, const char *loc) {      /* NUL-terminated copy in the arena */
    uint8_t *b = cb_arena_alloc((size_t)s.len + 1, loc);
    if (s.len) memcpy(b, s.ptr, (size_t)s.len);
    b[s.len] = 0;
    return b;
}
#endif
static inline cb_str cb_from_cstr(const char *p) {
    if (!p) return (cb_str){(const uint8_t *)"", 0};
    return (cb_str){(const uint8_t *)p, (int64_t)__builtin_strlen(p)};
}
static inline int64_t cb_slice_len(int64_t n, const char *loc) {
    if (CB_UNLIKELY(n < 0)) cb_panic(loc, "negative slice length");
    return n;
}

/* null checks for pointer dereference */
#ifdef CB_UNCHECKED
static inline void *cb_nn(void *p, const char *loc) { (void)loc; return p; }
#else
CB_COLD static void cb_panic_null(const char *loc) { cb_panic(loc, "null pointer dereference"); }
static inline void *cb_nn(void *p, const char *loc) { if (CB_UNLIKELY(!p)) cb_panic_null(loc); return p; }
#endif

#ifndef CB_NOSTD
/* ---- helpers behind std.time, std.fs and std.env ------------------------------------------- */
#include <time.h>
#include <errno.h>
#include <sys/stat.h>
static int64_t cb_time_mono_ns(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000000000LL + t.tv_nsec;
}
static int64_t cb_time_unix_ns(void) {
    struct timespec t; clock_gettime(CLOCK_REALTIME, &t);
    return (int64_t)t.tv_sec * 1000000000LL + t.tv_nsec;
}
static void cb_time_sleep_ns(int64_t ns) {
    if (ns <= 0) return;
    fflush(stdout);
    struct timespec t = { (time_t)(ns / 1000000000LL), (long)(ns % 1000000000LL) };
    while (nanosleep(&t, &t) == -1 && errno == EINTR) {}
}
static int64_t cb_time_local_offset(int64_t secs) {      /* seconds east of UTC at that moment */
    time_t t = (time_t)secs; struct tm tm;
    if (!localtime_r(&t, &tm)) return 0;
    return (int64_t)tm.tm_gmtoff;
}

static char *cb_path_c(cb_str s) {
    char *p = (char *)malloc((size_t)s.len + 1);
    if (!p) return NULL;
    if (s.len) memcpy(p, s.ptr, (size_t)s.len);
    p[s.len] = 0;
    return p;
}
/* 0 = ok, 1 = cannot open, 2 = read/write failed */
static int cb_fs_read(cb_str path, cb_String *out) {
    char *p = cb_path_c(path);
    if (!p) return 2;
    FILE *f = fopen(p, "rb");
    free(p);
    if (!f) return 1;
    cb_String s = {0};
    char buf[16384];
    size_t n;
    int rc = 0;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) s = cb_string_append(s, (cb_str){(const uint8_t *)buf, (int64_t)n}, "std.fs");
    if (ferror(f)) rc = 2;
    fclose(f);
    *out = s;
    return rc;
}
static int cb_fs_write(cb_str path, cb_str data, int append) {
    char *p = cb_path_c(path);
    if (!p) return 2;
    FILE *f = fopen(p, append ? "ab" : "wb");
    free(p);
    if (!f) return 1;
    int rc = (data.len && fwrite(data.ptr, 1, (size_t)data.len, f) != (size_t)data.len) ? 2 : 0;
    if (fclose(f) != 0) rc = 2;
    return rc;
}
static int cb_fs_stat(cb_str path, int64_t *size) {     /* 0 = missing, 1 = file, 2 = directory */
    char *p = cb_path_c(path);
    if (!p) return 0;
    struct stat st;
    int ok = stat(p, &st) == 0;
    free(p);
    if (!ok) return 0;
    *size = (int64_t)st.st_size;
    return S_ISDIR(st.st_mode) ? 2 : 1;
}
static int cb_fs_remove(cb_str path) {
    char *p = cb_path_c(path);
    if (!p) return 1;
    int rc = remove(p);
    free(p);
    return rc;
}
static int cb_fs_mkdir(cb_str path) {
    char *p = cb_path_c(path);
    if (!p) return 1;
    int rc = mkdir(p, 0777);
    free(p);
    return rc;
}

static int cb_argc_v;
static char **cb_argv_v;
static int64_t cb_argc(void) { return cb_argc_v; }
static cb_str cb_argv_at(int64_t i) { return cb_from_cstr(cb_argv_v[i]); }
static int cb_env_get(cb_str name, cb_String *out) {
    char *p = cb_path_c(name);
    if (!p) return 0;
    const char *v = getenv(p);
    free(p);
    if (!v) return 0;
    *out = cb_string_from(cb_from_cstr(v), "std.env");
    return 1;
}
#endif

#ifndef CB_NOSTD
/* helpers behind std.io */
static int cb_peekc(void) { int c = getchar(); if (c != EOF) ungetc(c, stdin); return c; }
static void cb_flush(void) { fflush(stdout); }
#endif
/* ---- end runtime ----------------------------------------------------- */
