/* ---- Crumbs runtime ---------------------------------------------------- */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE 1
#endif
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#ifndef CB_NOSTD            /* --no-stdruntime: only the freestanding headers above are used */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#if defined(CB_GC) || defined(CB_PYEXT)
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
/* a closure value: code pointer, captured environment (heap block or null), and the function that destroys the environment */
typedef struct { void *fn; void *env; void (*drop)(void *); } cb_Clo;
static inline void cb_clo_drop(cb_Clo *c) { if (c->env) { c->drop(c->env); c->env = 0; } }

#ifdef CB_NOSTD
#define cb_wb_range(p, n) ((void)0)       /* no collector, so no write barrier */
#define cb_gc_poll() ((void)0)
/* no libc: a panic just traps (there is nothing to print with) */
CB_COLD static void cb_panic(const char *loc, const char *msg) { (void)loc; (void)msg; __builtin_trap(); }
CB_COLD static void cb_panic_str(const char *loc, cb_str m) { (void)loc; (void)m; __builtin_trap(); }
CB_COLD static void cb_panic_idx(const char *loc, int64_t i, int64_t n) { (void)loc; (void)i; (void)n; __builtin_trap(); }
CB_COLD static void cb_panic_rng(const char *loc, int64_t lo, int64_t hi, int64_t n) { (void)loc; (void)lo; (void)hi; (void)n; __builtin_trap(); }
#elif defined(CB_PYEXT)
/* a Python extension: a panic unwinds to the wrapper that called into Crumbs, which raises `Panic` (destructors are skipped) */
static __thread jmp_buf *cb_py_jmp;
static __thread char cb_py_msg[512];
CB_COLD static void cb_py_unwind(const char *loc) {
    size_t n = strlen(cb_py_msg);
    snprintf(cb_py_msg + n, sizeof cb_py_msg - n, " (at %s)", loc);
    if (cb_py_jmp) longjmp(*cb_py_jmp, 1);
    fprintf(stderr, "panic: %s\n", cb_py_msg);
    exit(101);
}
CB_COLD static void cb_panic(const char *loc, const char *msg) { snprintf(cb_py_msg, sizeof cb_py_msg, "%s", msg); cb_py_unwind(loc); }
CB_COLD static void cb_panic_str(const char *loc, cb_str m) { snprintf(cb_py_msg, sizeof cb_py_msg, "%.*s", (int)m.len, (const char *)m.ptr); cb_py_unwind(loc); }
CB_COLD static void cb_panic_idx(const char *loc, int64_t i, int64_t n) {
    snprintf(cb_py_msg, sizeof cb_py_msg, "index out of bounds: the length is %lld but the index is %lld", (long long)n, (long long)i);
    cb_py_unwind(loc);
}
CB_COLD static void cb_panic_rng(const char *loc, int64_t lo, int64_t hi, int64_t n) {
    if (lo > hi) snprintf(cb_py_msg, sizeof cb_py_msg, "slice start %lld is greater than end %lld", (long long)lo, (long long)hi);
    else snprintf(cb_py_msg, sizeof cb_py_msg, "slice range %lld..%lld is out of bounds for length %lld", (long long)lo, (long long)hi, (long long)n);
    cb_py_unwind(loc);
}
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
/* the string arena (see below): per thread, so string temporaries need no lock. It lives up here because the collector
   must treat each thread's current chunk as a root (thread-local data is not part of the data segment). */
static __thread struct { uint8_t *top, *end; } cb_arena;
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
#ifdef CB_GC
static void cb_gc_lock(void); static void cb_gc_unlock(void);      /* the region list is shared with the collector */
#else
#define cb_gc_lock() ((void)0)
#define cb_gc_unlock() ((void)0)
#endif
#define CB_REGION_CHUNK ((size_t)64 << 10)
static cb_Region *cb_region_new(void) {
    cb_Region *r = (cb_Region *)calloc(1, sizeof *r);
    if (!r) { fputs("panic: out of memory (region)\n", stderr); exit(101); }
    cb_gc_lock();
    r->next = cb_regions;
    if (cb_regions) cb_regions->prev = r;
    cb_regions = r;
    cb_gc_unlock();
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
    cb_gc_lock();
    if (r->prev) r->prev->next = r->next; else cb_regions = r->next;
    if (r->next) r->next->prev = r->prev;
    cb_gc_unlock();
    free(r);
}

#ifndef CB_GC
static void *cb_mem_alloc(size_t n, int atomic) { (void)atomic; return calloc(n ? n : 1, 1); }
static void *cb_mem_realloc(void *p, size_t old, size_t n) { (void)old; return realloc(p, n); }
static void cb_mem_free(void *p) { free(p); }
static void cb_gc_collect(void) {}
static int64_t cb_gc_bytes(void) { return 0; }
static int64_t cb_gc_collections(void) { return 0; }
static void cb_gc_collect_gen(int64_t g, const char *loc) { (void)g; (void)loc; }
static int64_t cb_gc_gen_bytes(int64_t g, const char *loc) { (void)g; (void)loc; return 0; }
static int64_t cb_gc_gen_collections(int64_t g, const char *loc) { (void)g; (void)loc; return 0; }
#define cb_wb_range(p, n) ((void)0)
#define cb_gc_poll() ((void)0)
void cb_gc_write_barrier(const void *p, size_t n) { (void)p; (void)n; }
#define CB_GC_INIT() ((void)0)
/* without the collector there is nothing for threads to coordinate with */
void *cb_gc_thread_prepare(void *arg) { (void)arg; return 0; }
void cb_gc_thread_begin(void *rec) { (void)rec; }
void cb_gc_thread_end(void) {}
void cb_gc_blocking_begin(void) {}
void cb_gc_blocking_end(void) {}
#else
/* ---- the collector: three generations, tri-colour marking, thread-aware ----------------------------
   Every block has a 32-byte header in a doubly linked list, one list per generation:
       generation 0  the nursery: everything allocated since the last collection
       generation 1  the middle: survivors of one minor collection
       generation 2  the old generation: survivors of a mid or major collection
   The header also carries a colour while a collection runs:
       white  not reached (yet)       grey  reached, contents not scanned yet       black  reached and scanned

   Three kinds of collection (the allocator picks one when generation 0 has grown by CB_GC_NURSERY bytes):
       minor  collects generation 0.        Survivors move to generation 1.
       mid    collects generations 0 and 1. Survivors move to generation 2.      (every CB_GC_MID_EVERY minors, when
                                                                                  generation 1 is large, or when many
                                                                                  cards are dirty)
       major  collects everything. Survivors of 0 and 1 move to generation 2; generation 2 is swept lazily.
   gc_collect() runs a major collection and finishes the sweep.

   Three modes (chosen at build time, see --gc-mode):
       auto    (default) the allocator starts a collection itself when the nursery is full.
       manual  nothing is collected unless the program asks (gc_collect, gc_collect_gen), except as a last resort
               when the system is out of memory. For programs that want to choose their own pause points.
       thread  a background collector thread wakes every CB_GC_INTERVAL_MS milliseconds (and early when the nursery
               is full) and collects. Threads are stopped at safepoints: the compiler emits a cheap poll at every
               function entry and loop iteration, plus allocations and blocking calls are safepoints too. Allocation
               only collects by itself as an emergency, when the nursery has grown to 8 times its limit.

   A collection marks only the generations it collects: their blocks are painted white and put in a sorted index,
   and a pointer is followed only if it lands inside an indexed block. A pointer into an older generation is
   ignored (that block is assumed alive), so the cost of a minor collection depends on the amount of young data
   and not on the size of the heap. The roots are the machine stacks and saved registers of every thread, the
   data/bss segment, the live regions and the thread records, plus the OLD-TO-YOUNG POINTERS.

   Old-to-young pointers are found with a write barrier. The compiler emits cb_wb_range(&slot, size) after every
   store of a value that can hold a pointer into memory that is not a plain local variable (assignments through
   pointers, slices and fields, write_at). The barrier sets a bit in a card table (one bit per 512 bytes of
   address space). A minor or mid collection scans the dirty cards as roots, except the parts of a card that
   lie inside the blocks being collected (those are scanned only if the block is reached). A mid or major
   collection leaves nothing young behind, so it clears the table; a minor one keeps the cards that lie in
   generation 1 or 2. Compile with -DCB_GC_VERIFY to check every collection against a full trace from the roots
   (it aborts if a block that is reachable was left white, which is what a missing barrier looks like).
   Stores made by C code into Crumbs memory are not seen: call cb_gc_write_barrier(p, size) after them.

   Marking is stop-the-world, so no barrier is needed for the marking itself. Interior pointers keep a block
   alive. Memory handed to C and kept only there is NOT seen by the collector: keep a Crumbs reference to it.
   Young generations are swept eagerly at the end of a collection (they are small); after a major collection
   generation 2 is swept lazily: a cursor frees a few white blocks per allocation, and the next major
   collection finishes whatever is left.

   Threads (std.thread): once the first thread is created a heap lock is switched on. A collection waits
   (at most 100 ms) until every other thread is parked -- inside an allocation waiting for the lock, or in a
   blocking region (cb_gc_blocking_begin/end, which std.thread wraps around join, lock, wait, sleep). If a
   thread is still busy computing, the collection is skipped and tried again after more allocation.        */
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <errno.h>
#include <time.h>
typedef struct cb_hdr { size_t n; struct cb_hdr *prev, *next; uintptr_t flags; } cb_hdr;   /* flags: bit 1 atomic, bits 2-3 colour, bits 4-5 generation, bit 6 verify mark */
#define CB_HDR_ATOMIC 2u
#define CB_HDR_VMARK 0x40u
#define CB_WHITE 0u
#define CB_GREY 1u
#define CB_BLACK 2u
#define CB_COLOR(h) ((unsigned)(((h)->flags >> 2) & 3u))
#define CB_SETCOLOR(h, c) ((h)->flags = ((h)->flags & ~(uintptr_t)12) | ((uintptr_t)(c) << 2))
#define CB_GEN(h) ((unsigned)(((h)->flags >> 4) & 3u))
#define CB_SETGEN(h, g) ((h)->flags = ((h)->flags & ~(uintptr_t)0x30) | ((uintptr_t)(g) << 4))

#ifndef CB_GC_NURSERY
#define CB_GC_NURSERY ((size_t)4 << 20)      /* bytes allocated since the last collection that trigger a minor one */
#endif
#ifndef CB_GC_MID
#define CB_GC_MID ((size_t)16 << 20)         /* bytes in generation 1 that make the next collection a mid one */
#endif
#ifndef CB_GC_MID_EVERY
#define CB_GC_MID_EVERY 8                    /* ... or this many minor collections in a row */
#endif
#ifndef CB_GC_MAJOR_MIN
#define CB_GC_MAJOR_MIN ((size_t)16 << 20)   /* generation 2 must grow by at least this much (and by as much as it held) before a major one */
#endif
#ifndef CB_GC_INTERVAL_MS
#define CB_GC_INTERVAL_MS 100                /* thread mode: how often the collector thread wakes up */
#endif
#ifndef CB_GC_CARD_BUDGET
#define CB_GC_CARD_BUDGET 8192               /* dirty cards in old memory that make the next collection a mid one */
#endif

extern char __data_start[] __attribute__((weak));
extern char _end[] __attribute__((weak));
static cb_hdr *cb_gc_head[3], *cb_gc_cursor;     /* a list per generation; the lazy-sweep cursor walks generation 2 */
static size_t cb_gc_gbytes[3], cb_gc_live, cb_gc_marked, cb_gc_runs[3];
static size_t cb_gc_trigger = CB_GC_NURSERY, cb_gc_g2_grow, cb_gc_g2_limit = CB_GC_MAJOR_MIN, cb_gc_ndirty;
static unsigned cb_gc_minors_since_mid;
static int cb_gc_force_major;
#ifdef CB_GC_THREAD
#define cb_gc_collector_owns_sweep 1     /* thread mode: the collector thread does the lazy sweep, allocations do not */
#else
#define cb_gc_collector_owns_sweep 0
#endif
static void *cb_gc_stack_bottom;
static cb_hdr **cb_gc_idx; static size_t cb_gc_nidx;      /* sorted index of the blocks being collected */
static cb_hdr **cb_gc_work; static size_t cb_gc_nwork, cb_gc_capwork;
#ifdef CB_GC_THREAD
static void cb_gc_collector_start(void);
static volatile int cb_gc_stop;                  /* a collection is waiting for every thread to reach a safepoint */
static void cb_gc_park(void);
static void cb_gc_request(void);
#define CB_GC_INIT() (cb_gc_stack_bottom = __builtin_frame_address(0), cb_gc_collector_start())
#define cb_gc_poll() do { if (CB_UNLIKELY(cb_gc_stop)) cb_gc_park(); } while (0)
#else
#define CB_GC_INIT() (cb_gc_stack_bottom = __builtin_frame_address(0))
#define cb_gc_poll() ((void)0)
#endif

/* ---- the card table: one bit per 512 bytes of address space, in leaves of 2^21 cards (1 GiB) ---- */
#define CB_CARD_SHIFT 9
#define CB_CARD_LEAF_BITS 21
#define CB_CARD_TOP ((uintptr_t)1 << 18)
static uint8_t *cb_card_leaf[CB_CARD_TOP];
static uint8_t *cb_card_new_leaf(uintptr_t top) {
    uint8_t *l = (uint8_t *)calloc(1, (size_t)1 << (CB_CARD_LEAF_BITS - 3)), *old = 0;
    if (!l) return 0;
    if (!__atomic_compare_exchange_n(&cb_card_leaf[top], &old, l, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) { free(l); return old; }
    return l;
}
static inline void cb_wb_card(uintptr_t card) {
    uintptr_t top = card >> CB_CARD_LEAF_BITS;
    if (CB_UNLIKELY(top >= CB_CARD_TOP)) { cb_gc_force_major = 1; return; }       /* not an address we can track */
    uint8_t *leaf = cb_card_leaf[top];
    if (CB_UNLIKELY(!leaf)) { leaf = cb_card_new_leaf(top); if (!leaf) { cb_gc_force_major = 1; return; } }
    size_t i = (size_t)(card & (((uintptr_t)1 << CB_CARD_LEAF_BITS) - 1));
    uint8_t bit = (uint8_t)(1u << (i & 7));
    if (!(leaf[i >> 3] & bit)) { __atomic_fetch_or(&leaf[i >> 3], bit, __ATOMIC_RELAXED); __atomic_fetch_add(&cb_gc_ndirty, 1, __ATOMIC_RELAXED); }
}
static inline void cb_wb_range(const void *p, size_t n) {         /* the write barrier: `n` bytes at `p` were just stored */
    uintptr_t a = (uintptr_t)p >> CB_CARD_SHIFT, z = ((uintptr_t)p + (n ? n : 1) - 1) >> CB_CARD_SHIFT;
    for (;;) { cb_wb_card(a); if (a >= z) break; a++; }
}
void cb_gc_write_barrier(const void *p, size_t n) { cb_wb_range(p, n); }   /* for C code that stores into Crumbs memory */

/* ---- threads ---- */
enum { CB_T_RUNNING = 0, CB_T_SAFE = 1, CB_T_NEW = 2 };
typedef struct cb_gc_thread {
    struct cb_gc_thread *next;
    void *arg;                    /* start argument, kept alive until the thread has its own copy */
    void *sp, *bottom;            /* saved stack pointer (while SAFE) and the stack base */
    void *arena;                  /* this thread's string arena (thread-local, so the collector cannot find it by itself) */
    int state;
    jmp_buf regs;                 /* saved callee-saved registers */
} cb_gc_thread;
static pthread_mutex_t cb_gc_mu = PTHREAD_MUTEX_INITIALIZER;
static int cb_gc_mt;                          /* 1 once a second thread exists: the heap lock is then used */
static cb_gc_thread *cb_gc_threads;
static __thread cb_gc_thread *cb_gc_self;

__attribute__((noinline)) static void cb_gc_lock_slow(void) {
    cb_gc_thread *t = cb_gc_self;
    volatile char probe;
    if (t) { setjmp(t->regs); t->sp = (void *)&probe; __atomic_store_n(&t->state, CB_T_SAFE, __ATOMIC_RELEASE); }
    pthread_mutex_lock(&cb_gc_mu);
    if (t) __atomic_store_n(&t->state, CB_T_RUNNING, __ATOMIC_RELEASE);
}
static void cb_gc_lock(void) {
    if (CB_LIKELY(!cb_gc_mt)) return;
    if (pthread_mutex_trylock(&cb_gc_mu) != 0) cb_gc_lock_slow();
}
static void cb_gc_unlock(void) { if (CB_UNLIKELY(cb_gc_mt)) pthread_mutex_unlock(&cb_gc_mu); }

/* public thread hooks, bound by std.thread through `extern fn` */
void *cb_gc_thread_prepare(void *arg) {           /* called by the spawner, before pthread_create */
    if (!cb_gc_mt) {
        cb_gc_thread *m = (cb_gc_thread *)calloc(1, sizeof *m);
        m->bottom = cb_gc_stack_bottom ? cb_gc_stack_bottom : __builtin_frame_address(0);
        m->state = CB_T_RUNNING; m->arena = &cb_arena;
        cb_gc_threads = m; cb_gc_self = m;
        cb_gc_mt = 1;
    }
    cb_gc_thread *t = (cb_gc_thread *)calloc(1, sizeof *t);
    t->arg = arg; t->state = CB_T_NEW;
    cb_gc_lock();
    t->next = cb_gc_threads; cb_gc_threads = t;
    cb_gc_unlock();
    return t;
}
void cb_gc_thread_begin(void *rec) {              /* first thing a new thread does */
    cb_gc_thread *t = (cb_gc_thread *)rec;
    pthread_attr_t at; void *addr = 0; size_t size = 0;
    if (pthread_getattr_np(pthread_self(), &at) == 0) { pthread_attr_getstack(&at, &addr, &size); pthread_attr_destroy(&at); }
    cb_gc_self = t;
    t->arena = &cb_arena;
    t->bottom = addr ? (char *)addr + size : __builtin_frame_address(0);
    cb_gc_lock_slow();                            /* waits out a running collection, then RUNNING */
    pthread_mutex_unlock(&cb_gc_mu);
}
void cb_gc_thread_end(void) {                     /* last thing a thread does */
    cb_gc_thread *t = cb_gc_self;
    if (!t) return;
    cb_gc_lock_slow();
    for (cb_gc_thread **p = &cb_gc_threads; *p; p = &(*p)->next) if (*p == t) { *p = t->next; break; }
    pthread_mutex_unlock(&cb_gc_mu);
    cb_gc_self = 0;
    free(t);
}
__attribute__((noinline)) void cb_gc_blocking_begin(void) {   /* about to block in C: let a collection go ahead */
    cb_gc_thread *t = cb_gc_self;
    volatile char probe;
    if (!t) return;
    setjmp(t->regs); t->sp = (void *)&probe;
    __atomic_store_n(&t->state, CB_T_SAFE, __ATOMIC_RELEASE);
}
void cb_gc_blocking_end(void) {                   /* back from C: wait for a running collection to finish */
    cb_gc_thread *t = cb_gc_self;
    if (!t) return;
    pthread_mutex_lock(&cb_gc_mu);
    __atomic_store_n(&t->state, CB_T_RUNNING, __ATOMIC_RELEASE);
    pthread_mutex_unlock(&cb_gc_mu);
}

/* ---- lists ---- */
static void cb_gc_link(cb_hdr *h, unsigned g) {
    CB_SETGEN(h, g);
    h->prev = 0; h->next = cb_gc_head[g];
    if (h->next) h->next->prev = h;
    cb_gc_head[g] = h;
}
static void cb_gc_unlink(cb_hdr *h) {
    if (h == cb_gc_cursor) cb_gc_cursor = h->next;
    if (h->prev) h->prev->next = h->next; else cb_gc_head[CB_GEN(h)] = h->next;
    if (h->next) h->next->prev = h->prev;
}

/* ---- marking ---- */
static int cb_gc_cmp(const void *a, const void *b) {
    uintptr_t x = (uintptr_t)*(cb_hdr *const *)a, y = (uintptr_t)*(cb_hdr *const *)b;
    return x < y ? -1 : x > y;
}
static inline uintptr_t cb_gc_ext_end(const cb_hdr *h) { return (uintptr_t)(h + 1) + (h->n ? h->n : 1); }
static cb_hdr *cb_gc_find(uintptr_t w) {
    size_t lo = 0, hi = cb_gc_nidx;
    while (lo < hi) {                       /* last block whose data start is <= w */
        size_t mid = (lo + hi) / 2;
        if ((uintptr_t)(cb_gc_idx[mid] + 1) <= w) lo = mid + 1; else hi = mid;
    }
    if (!lo) return 0;
    cb_hdr *h = cb_gc_idx[lo - 1];
    return w < cb_gc_ext_end(h) ? h : 0;
}
static void cb_gc_shade(uintptr_t w) {      /* white -> grey (or straight to black if it holds no pointers) */
    cb_hdr *h = cb_gc_find(w);
    if (!h || CB_COLOR(h) != CB_WHITE) return;
    cb_gc_marked += h->n;
    if (h->flags & CB_HDR_ATOMIC) { CB_SETCOLOR(h, CB_BLACK); return; }
    CB_SETCOLOR(h, CB_GREY);
    if (cb_gc_nwork == cb_gc_capwork) {
        cb_gc_capwork = cb_gc_capwork ? cb_gc_capwork * 2 : 1024;
        cb_gc_work = (cb_hdr **)realloc(cb_gc_work, cb_gc_capwork * sizeof *cb_gc_work);
        if (!cb_gc_work) { fputs("panic: out of memory (gc)\n", stderr); exit(101); }
    }
    cb_gc_work[cb_gc_nwork++] = h;
}
__attribute__((no_sanitize("address"))) static void cb_gc_scan(const void *lo, const void *hi) {
    uintptr_t a = ((uintptr_t)lo + sizeof(void *) - 1) & ~(uintptr_t)(sizeof(void *) - 1);
    for (; a + sizeof(void *) <= (uintptr_t)hi; a += sizeof(void *)) cb_gc_shade(*(const uintptr_t *)a);
}
#ifdef CB_GC_VERIFY
static uintptr_t *cb_v_roots; static size_t cb_v_nroots, cb_v_caproots;       /* every word the root scan looked at */
__attribute__((no_sanitize("address"))) static void cb_gc_scan_root(const void *lo, const void *hi) {
    uintptr_t a = ((uintptr_t)lo + sizeof(void *) - 1) & ~(uintptr_t)(sizeof(void *) - 1);
    for (; a + sizeof(void *) <= (uintptr_t)hi; a += sizeof(void *)) {
        uintptr_t w = *(const uintptr_t *)a;
        if (cb_v_nroots == cb_v_caproots) {
            cb_v_caproots = cb_v_caproots ? cb_v_caproots * 2 : 4096;
            cb_v_roots = (uintptr_t *)realloc(cb_v_roots, cb_v_caproots * sizeof *cb_v_roots);
            if (!cb_v_roots) abort();
        }
        cb_v_roots[cb_v_nroots++] = w;
        cb_gc_shade(w);
    }
}
#else
#define cb_gc_scan_root cb_gc_scan
#endif
static void cb_gc_scan_range(const void *a, const void *b) { if (a < b) cb_gc_scan_root(a, b); else cb_gc_scan_root(b, a); }
static void cb_region_roots(void) {
    for (cb_Region *r = cb_regions; r; r = r->next)
        for (cb_RegionChunk *c = r->chunks; c; c = c->next)
            cb_gc_scan_root((const void *)(c + 1), (const uint8_t *)(c + 1) + 16 + c->used);
}

/* ---- dirty cards ---- */
static sigjmp_buf cb_gc_fault_jmp;
static volatile sig_atomic_t cb_gc_in_card;
static struct sigaction cb_gc_old_segv, cb_gc_old_bus;
static void cb_gc_fault(int sig, siginfo_t *si, void *uc) {
    (void)si; (void)uc;
    if (cb_gc_in_card) { cb_gc_in_card = 0; siglongjmp(cb_gc_fault_jmp, 1); }
    sigaction(sig, sig == SIGSEGV ? &cb_gc_old_segv : &cb_gc_old_bus, 0);   /* not ours: let it fault again, into the old handler */
}
/* Scan one dirty card, skipping the parts inside the blocks being collected. Returns 1 if the whole card lies
   inside one of those blocks (its contents were written while the block was young, so the dirt is obsolete). */
static int cb_gc_scan_card(uintptr_t cs) {
    uintptr_t ce = cs + ((uintptr_t)1 << CB_CARD_SHIFT);
    size_t lo = 0, hi = cb_gc_nidx;
    while (lo < hi) {                               /* first indexed block that ends after the card starts */
        size_t mid = (lo + hi) / 2;
        if (cb_gc_ext_end(cb_gc_idx[mid]) <= cs) lo = mid + 1; else hi = mid;
    }
    uintptr_t a = cs;
    for (size_t i = lo; i < cb_gc_nidx && a < ce; i++) {
        cb_hdr *h = cb_gc_idx[i];
        uintptr_t bs = (uintptr_t)h, be = cb_gc_ext_end(h);
        if (bs >= ce) break;
        if (bs <= cs && be >= ce) return 1;
        if (bs > a) cb_gc_scan((const void *)a, (const void *)bs);
        if (be > a) a = be;
    }
    if (a < ce) cb_gc_scan((const void *)a, (const void *)ce);
    return 0;
}
static void cb_gc_scan_cards(void) {
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = cb_gc_fault; sa.sa_flags = SA_SIGINFO | SA_NODEFER; sigemptyset(&sa.sa_mask);
    int armed = 0;
    for (uintptr_t top = 0; top < CB_CARD_TOP; top++) {
        uint8_t *leaf = cb_card_leaf[top];
        if (!leaf) continue;
        const size_t words = ((size_t)1 << (CB_CARD_LEAF_BITS - 3)) / sizeof(uint64_t);
        for (size_t w = 0; w < words; w++) {
            uint64_t bits;
            memcpy(&bits, leaf + w * sizeof bits, sizeof bits);
            if (!bits) continue;
            if (!armed) { sigaction(SIGSEGV, &sa, &cb_gc_old_segv); sigaction(SIGBUS, &sa, &cb_gc_old_bus); armed = 1; }
            for (unsigned b = 0; b < 64; b++) {
                if (!(bits >> b & 1)) continue;
                uintptr_t card = (top << CB_CARD_LEAF_BITS) | (w * 64 + b);
                int obsolete = 0;
                if (sigsetjmp(cb_gc_fault_jmp, 0) == 0) {          /* a stale card may sit on memory that was unmapped since */
                    cb_gc_in_card = 1;
                    obsolete = cb_gc_scan_card(card << CB_CARD_SHIFT);
                    cb_gc_in_card = 0;
                } else obsolete = 1;
                if (obsolete) {
                    leaf[(w * 64 + b) >> 3] &= (uint8_t)~(1u << (b & 7));
                    if (cb_gc_ndirty) cb_gc_ndirty--;
                }
            }
        }
    }
    if (armed) { sigaction(SIGSEGV, &cb_gc_old_segv, 0); sigaction(SIGBUS, &cb_gc_old_bus, 0); }
}
static void cb_gc_clear_cards(void) {
    for (uintptr_t top = 0; top < CB_CARD_TOP; top++) {
        uint8_t *leaf = cb_card_leaf[top];
        if (!leaf) continue;
        const size_t words = ((size_t)1 << (CB_CARD_LEAF_BITS - 3)) / sizeof(uint64_t);
        for (size_t w = 0; w < words; w++) {
            uint64_t bits;
            memcpy(&bits, leaf + w * sizeof bits, sizeof bits);
            if (bits) memset(leaf + w * sizeof bits, 0, sizeof bits);
        }
    }
    cb_gc_ndirty = 0;
}

/* ---- sweeping ---- */
static void cb_gc_sweep_step(size_t budget) {            /* lazy sweep of generation 2 */
    while (cb_gc_cursor && budget--) {
        cb_hdr *h = cb_gc_cursor;
        cb_gc_cursor = h->next;
        if (CB_COLOR(h) == CB_WHITE) {
            cb_gc_unlink(h);
            cb_gc_live -= h->n; cb_gc_gbytes[2] -= h->n;
            free(h);
        }
    }
}
static size_t cb_gc_sweep_young(int level) {             /* free the white blocks of the collected generations, promote the rest */
    size_t promoted = 0;
    for (int g = 0; g <= level && g < 2; g++) {
        unsigned dest = level >= 1 ? 2u : 1u;
        cb_hdr *h = cb_gc_head[g];
        cb_gc_head[g] = 0; cb_gc_gbytes[g] = 0;
        while (h) {
            cb_hdr *nx = h->next;
            if (CB_COLOR(h) == CB_WHITE) { cb_gc_live -= h->n; free(h); }
            else { cb_gc_link(h, dest); cb_gc_gbytes[dest] += h->n; promoted += h->n; }
            h = nx;
        }
    }
    return promoted;
}

#ifdef CB_GC_VERIFY
/* Generational check: trace the whole heap from the same root words, ignoring generations. A block of a collected
   generation that this trace reaches but the collection left white was lost: some old-to-young pointer was not
   recorded by a write barrier. Also: no grey block may be left. */
static cb_hdr **cb_v_idx; static size_t cb_v_n;
static cb_hdr *cb_v_find(uintptr_t w) {
    size_t lo = 0, hi = cb_v_n;
    while (lo < hi) { size_t mid = (lo + hi) / 2; if ((uintptr_t)(cb_v_idx[mid] + 1) <= w) lo = mid + 1; else hi = mid; }
    if (!lo) return 0;
    cb_hdr *h = cb_v_idx[lo - 1];
    return w < cb_gc_ext_end(h) ? h : 0;
}
__attribute__((no_sanitize("address"))) static void cb_gc_verify(int level) {
    cb_v_n = 0;
    for (int g = 0; g < 3; g++) for (cb_hdr *h = cb_gc_head[g]; h; h = h->next) cb_v_n++;
    cb_v_idx = (cb_hdr **)malloc((cb_v_n ? cb_v_n : 1) * sizeof *cb_v_idx);
    cb_hdr **stack = (cb_hdr **)malloc((cb_v_n ? cb_v_n : 1) * sizeof *stack);
    if (!cb_v_idx || !stack) abort();
    size_t k = 0, sp = 0;
    for (int g = 0; g < 3; g++) for (cb_hdr *h = cb_gc_head[g]; h; h = h->next) { cb_v_idx[k++] = h; h->flags &= ~(uintptr_t)CB_HDR_VMARK; }
    qsort(cb_v_idx, cb_v_n, sizeof *cb_v_idx, cb_gc_cmp);
    for (size_t i = 0; i < cb_v_n; i++) if (CB_GEN(cb_v_idx[i]) <= (unsigned)level && CB_COLOR(cb_v_idx[i]) == CB_GREY) {
        fputs("gc verify: grey block left after marking\n", stderr); abort();
    }
    size_t i = 0, nroots = cb_v_nroots;
    for (;;) {
        uintptr_t w;
        if (i < nroots) w = cb_v_roots[i++];
        else if (sp) {
            cb_hdr *h = stack[--sp];
            for (uintptr_t a = (uintptr_t)(h + 1); a + sizeof(void *) <= (uintptr_t)(h + 1) + h->n; a += sizeof(void *)) {
                cb_hdr *t = cb_v_find(*(const uintptr_t *)a);
                if (!t || (t->flags & CB_HDR_VMARK)) continue;
                t->flags |= CB_HDR_VMARK;
                if (CB_GEN(t) <= (unsigned)level && CB_COLOR(t) == CB_WHITE) {
                    fprintf(stderr, "gc verify: a %u-byte block of generation %u is reachable but was not marked (missing write barrier?)\n", (unsigned)t->n, CB_GEN(t));
                    abort();
                }
                if (!(t->flags & CB_HDR_ATOMIC)) stack[sp++] = t;
            }
            continue;
        } else break;
        cb_hdr *t = cb_v_find(w);
        if (!t || (t->flags & CB_HDR_VMARK)) continue;
        t->flags |= CB_HDR_VMARK;
        if (CB_GEN(t) <= (unsigned)level && CB_COLOR(t) == CB_WHITE) {
            fprintf(stderr, "gc verify: a %u-byte block of generation %u is reachable from a root but was not marked\n", (unsigned)t->n, CB_GEN(t));
            abort();
        }
        if (!(t->flags & CB_HDR_ATOMIC)) stack[sp++] = t;
    }
    for (size_t j = 0; j < cb_v_n; j++) cb_v_idx[j]->flags &= ~(uintptr_t)CB_HDR_VMARK;
    free(stack); free(cb_v_idx); cb_v_idx = 0; cb_v_n = 0;
    cb_v_nroots = 0;
}
#endif

/* ---- a cycle ---- */
static int cb_gc_stop_world(void) {
    if (!cb_gc_mt) return 1;
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    for (;;) {
        int all = 1;
        for (cb_gc_thread *t = cb_gc_threads; t; t = t->next)
            if (t != cb_gc_self && __atomic_load_n(&t->state, __ATOMIC_ACQUIRE) == CB_T_RUNNING) all = 0;
        if (all) return 1;
        clock_gettime(CLOCK_MONOTONIC, &t1);
        if ((t1.tv_sec - t0.tv_sec) * 1000000000L + (t1.tv_nsec - t0.tv_nsec) > 100000000L) return 0;
        sched_yield();
    }
}
__attribute__((noinline, no_sanitize("address"))) static void cb_gc_roots(void) {
    jmp_buf regs;                            /* spill callee-saved registers onto the stack */
    setjmp(regs);
    void *sp = (void *)&regs;
    if (cb_gc_self) {
        cb_gc_scan_range(sp, cb_gc_self->bottom);
        for (cb_gc_thread *t = cb_gc_threads; t; t = t->next) {
            cb_gc_scan_root(t, t + 1);       /* the record itself: saved registers and start argument */
            if (t->arena) cb_gc_scan_root(t->arena, (const uint8_t *)t->arena + sizeof cb_arena);   /* its string arena's chunk */
            if (t != cb_gc_self && t->state == CB_T_SAFE) cb_gc_scan_range(t->sp, t->bottom);
        }
    } else {
        if (cb_gc_stack_bottom) cb_gc_scan_range(sp, cb_gc_stack_bottom);
        cb_gc_scan_root(&cb_arena, &cb_arena + 1);
    }
    if (__data_start && _end) cb_gc_scan_root(__data_start, _end);
    cb_region_roots();                       /* pointers stored inside live regions keep their targets alive */
}
/* level 0 = minor, 1 = mid, 2 = major */
__attribute__((noinline, no_sanitize("address"))) static int cb_gc_collect_impl(int level) {
    if (cb_gc_force_major) level = 2;
    if (!cb_gc_stop_world()) { cb_gc_trigger = cb_gc_gbytes[0] + CB_GC_NURSERY; return 0; }   /* a thread is busy: try again later */
    if (level == 2) cb_gc_sweep_step((size_t)-1);                    /* finish the previous major's sweep before repainting */
    size_t n = 0;
    for (int g = 0; g <= level; g++) for (cb_hdr *h = cb_gc_head[g]; h; h = h->next) n++;
    cb_gc_idx = (cb_hdr **)malloc((n ? n : 1) * sizeof *cb_gc_idx);
    if (!cb_gc_idx) return 0;
    cb_gc_nidx = 0;
    for (int g = 0; g <= level; g++)
        for (cb_hdr *h = cb_gc_head[g]; h; h = h->next) { CB_SETCOLOR(h, CB_WHITE); cb_gc_idx[cb_gc_nidx++] = h; }
    qsort(cb_gc_idx, cb_gc_nidx, sizeof *cb_gc_idx, cb_gc_cmp);
    cb_gc_marked = 0;
    cb_gc_roots();                           /* roots */
    if (level < 2) cb_gc_scan_cards();       /* old-to-young pointers recorded by the write barrier */
    while (cb_gc_nwork) {                    /* grey -> black until no grey is left */
        cb_hdr *h = cb_gc_work[--cb_gc_nwork];
        cb_gc_scan(h + 1, (uint8_t *)(h + 1) + h->n);
        CB_SETCOLOR(h, CB_BLACK);
    }
#ifdef CB_GC_VERIFY
    cb_gc_verify(level);
#endif
    free(cb_gc_idx); cb_gc_idx = 0; cb_gc_nidx = 0;
    size_t promoted = cb_gc_sweep_young(level);
    if (level >= 1) cb_gc_clear_cards();     /* nothing young is left: every recorded old-to-young pointer is gone */
    if (level == 2) {
        cb_gc_cursor = cb_gc_head[2];        /* lazy sweep of the old generation starts */
        cb_gc_g2_grow = 0;
        cb_gc_g2_limit = cb_gc_marked > CB_GC_MAJOR_MIN ? cb_gc_marked : CB_GC_MAJOR_MIN;
        cb_gc_force_major = 0;
        cb_gc_minors_since_mid = 0;
    } else if (level == 1) {
        cb_gc_g2_grow += promoted;
        cb_gc_minors_since_mid = 0;
    } else cb_gc_minors_since_mid++;
    cb_gc_runs[level]++;
    cb_gc_trigger = CB_GC_NURSERY;
    return 1;
}
static int cb_gc_collect_locked(int level) {      /* returns 0 if it had to give up because a thread would not stop */
#ifdef CB_GC_THREAD
    cb_gc_stop = 1;                          /* ask every thread to park at its next safepoint */
#endif
    int ok = cb_gc_collect_impl(level);
#ifdef CB_GC_THREAD
    cb_gc_stop = 0;
#endif
    return ok;
}
static int cb_gc_pick_level(void) {
    if (cb_gc_force_major || cb_gc_g2_grow >= cb_gc_g2_limit) return 2;
    if (cb_gc_gbytes[1] >= CB_GC_MID || cb_gc_minors_since_mid >= CB_GC_MID_EVERY || cb_gc_ndirty >= CB_GC_CARD_BUDGET) return 1;
    return 0;
}
static void *cb_mem_alloc(size_t n, int atomic) {
    if (!cb_gc_stack_bottom) {              /* library mode: no main() ran CB_GC_INIT; approximate from here */
        extern void *__libc_stack_end __attribute__((weak));
        cb_gc_stack_bottom = &__libc_stack_end ? __libc_stack_end : __builtin_frame_address(0);
    }
#ifdef CB_GC_THREAD
    cb_gc_collector_start();                /* library mode: no main() ran CB_GC_INIT */
#endif
    cb_gc_lock();
#if defined(CB_GC_THREAD)
    if (cb_gc_gbytes[0] >= cb_gc_trigger) {
        if (cb_gc_gbytes[0] >= cb_gc_trigger * 8) cb_gc_collect_locked(cb_gc_pick_level());   /* the collector thread is not keeping up */
        else cb_gc_request();
    }
#elif !defined(CB_GC_MANUAL)
    if (cb_gc_gbytes[0] >= cb_gc_trigger) cb_gc_collect_locked(cb_gc_pick_level());
#endif
    if (cb_gc_cursor && !cb_gc_collector_owns_sweep) cb_gc_sweep_step(64);
    cb_hdr *h = (cb_hdr *)calloc(1, sizeof(cb_hdr) + (n ? n : 1));
    if (!h) { cb_gc_collect_locked(2); cb_gc_sweep_step((size_t)-1); h = (cb_hdr *)calloc(1, sizeof(cb_hdr) + (n ? n : 1)); }
    if (!h) { cb_gc_unlock(); return 0; }
    h->n = n; h->flags = atomic ? CB_HDR_ATOMIC : 0;
    cb_gc_link(h, 0);
    cb_gc_live += n; cb_gc_gbytes[0] += n;
    cb_gc_unlock();
    return h + 1;
}
static void cb_mem_free(void *p) {
    if (!p) return;
    cb_hdr *h = (cb_hdr *)p - 1;
    cb_gc_lock();
    cb_gc_unlink(h);
    cb_gc_live -= h->n; cb_gc_gbytes[CB_GEN(h)] -= h->n;
    cb_gc_unlock();
    free(h);
}
static void *cb_mem_realloc(void *p, size_t old, size_t n) {
    if (!p) return cb_mem_alloc(n, 1);
    cb_hdr *h = (cb_hdr *)p - 1;
    void *q = cb_mem_alloc(n, (int)((h->flags & CB_HDR_ATOMIC) != 0));
    if (!q) return 0;
    memcpy(q, p, old < n ? old : n);
    cb_mem_free(p);
    return q;
}
static void cb_gc_collect(void) {            /* a full collection: mark, then sweep to the end */
    cb_gc_lock();
    cb_gc_collect_locked(2);
    cb_gc_sweep_step((size_t)-1);
    cb_gc_unlock();
}
static void cb_gc_collect_gen(int64_t g, const char *loc) {      /* collect generations 0..g */
    if (g < 0 || g > 2) cb_panic(loc, "gc_collect_gen: the generation must be 0, 1 or 2");
    cb_gc_lock();
    cb_gc_collect_locked((int)g);
    if (g == 2) cb_gc_sweep_step((size_t)-1);
    cb_gc_unlock();
}
static int64_t cb_gc_bytes(void) { return (int64_t)cb_gc_live; }
static int64_t cb_gc_collections(void) { return (int64_t)(cb_gc_runs[0] + cb_gc_runs[1] + cb_gc_runs[2]); }
static int64_t cb_gc_gen_bytes(int64_t g, const char *loc) {
    if (g < 0 || g > 2) cb_panic(loc, "gc_gen_bytes: the generation must be 0, 1 or 2");
    return (int64_t)cb_gc_gbytes[g];
}
static int64_t cb_gc_gen_collections(int64_t g, const char *loc) {
    if (g < 0 || g > 2) cb_panic(loc, "gc_gen_collections: the generation must be 0, 1 or 2");
    return (int64_t)cb_gc_runs[g];
}

#ifdef CB_GC_THREAD
/* ---- thread mode: a background collector thread ---------------------------------------------------
   The thread is an ordinary registered thread. Every CB_GC_INTERVAL_MS milliseconds (or as soon as an allocation
   finds the nursery full) it takes the heap lock, raises cb_gc_stop and collects. Mutators park at their next
   safepoint (a poll, an allocation, or a blocking call). If a thread is stuck in a long C call the collection
   is given up after 100 ms and retried after a longer pause (up to 16 intervals), so a blocked thread
   cannot make the program pause every interval. */
static pthread_mutex_t cb_gc_wake_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cb_gc_wake_cv = PTHREAD_COND_INITIALIZER;
static int cb_gc_wanted, cb_gc_collector_up;
static void cb_gc_park(void) {                  /* a safepoint: wait here until the running collection is over */
    cb_gc_lock_slow();
    pthread_mutex_unlock(&cb_gc_mu);
}
static void cb_gc_request(void) {
    if (__atomic_load_n(&cb_gc_wanted, __ATOMIC_RELAXED)) return;
    pthread_mutex_lock(&cb_gc_wake_mu);
    cb_gc_wanted = 1;
    pthread_cond_signal(&cb_gc_wake_cv);
    pthread_mutex_unlock(&cb_gc_wake_mu);
}
static void *cb_gc_collector_main(void *rec) {
    cb_gc_thread_begin(rec);
    unsigned backoff = 1;
    for (;;) {
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        long ms = (long)CB_GC_INTERVAL_MS * (long)backoff;
        ts.tv_sec += ms / 1000; ts.tv_nsec += (ms % 1000) * 1000000L;
        if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
        cb_gc_blocking_begin();                 /* asleep: a collection started by someone else need not wait for us */
        pthread_mutex_lock(&cb_gc_wake_mu);
        while (!cb_gc_wanted && pthread_cond_timedwait(&cb_gc_wake_cv, &cb_gc_wake_mu, &ts) != ETIMEDOUT) {}
        cb_gc_wanted = 0;
        pthread_mutex_unlock(&cb_gc_wake_mu);
        cb_gc_blocking_end();
        cb_gc_lock();
        int ok = 1;
        if (cb_gc_gbytes[0] > 0 || cb_gc_force_major) ok = cb_gc_collect_locked(cb_gc_pick_level());
        if (ok && cb_gc_cursor) cb_gc_sweep_step(4096);
        cb_gc_unlock();
        backoff = ok ? 1 : (backoff < 16 ? backoff * 2 : 16);
        if (ok && cb_gc_cursor) backoff = 1;
    }
    return 0;
}
static void cb_gc_collector_start(void) {
    if (cb_gc_collector_up) return;
    cb_gc_collector_up = 1;
    void *rec = cb_gc_thread_prepare(0);
    pthread_t th;
    if (pthread_create(&th, 0, cb_gc_collector_main, rec) != 0) return;   /* no thread: allocation falls back to collecting by itself */
    pthread_detach(th);
}
#endif
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
/* ---- interpolation: f"..{value:spec}.." ----------------------------------
   Pieces are appended to a malloc'd buffer (cb_fb); the finished text is copied into the string arena. */
typedef struct { char fill, align, sign, type; uint8_t alt, zero, group; int32_t width, prec; } cb_fspec;
typedef struct { char *p; size_t len, cap; } cb_fb;
static void cb_fb_init(cb_fb *b) { b->p = NULL; b->len = b->cap = 0; }
static void cb_fb_free(cb_fb *b) { free(b->p); b->p = NULL; b->len = b->cap = 0; }
static void cb_fb_put(cb_fb *b, const void *s, size_t n) {
    if (b->len + n + 1 > b->cap) {
        size_t cap = b->cap ? b->cap * 2 : 64;
        while (cap < b->len + n + 1) cap *= 2;
        char *q = (char *)realloc(b->p, cap);
        if (!q) { fputs("out of memory while formatting a string\n", stderr); abort(); }
        b->p = q; b->cap = cap;
    }
    if (n) memcpy(b->p + b->len, s, n);
    b->len += n;
    b->p[b->len] = 0;
}
static void cb_fb_cstr(cb_fb *b, const char *s) { cb_fb_put(b, s, strlen(s)); }
static void cb_fb_fill(cb_fb *b, char c, size_t n) { while (n--) cb_fb_put(b, &c, 1); }
static cb_str cb_fb_finish(cb_fb *b, const char *loc) {
    uint8_t *buf = cb_arena_alloc(b->len ? b->len : 1, loc);
    if (b->len) memcpy(buf, b->p, b->len);
    cb_str r = { buf, (int64_t)b->len };
    cb_fb_free(b);
    return r;
}
static size_t cb_utf8_count(const char *s, size_t n) {
    size_t c = 0;
    for (size_t i = 0; i < n; i++) if (((uint8_t)s[i] & 0xC0) != 0x80) c++;
    return c;
}
/* pad `s` (n bytes, the first `prefix` being a sign / 0x that zero padding goes after) to the spec's width */
static void cb_fpad(cb_fb *b, const char *s, size_t n, const cb_fspec *f, int numeric, size_t prefix) {
    size_t chars = cb_utf8_count(s, n);
    if (f->width <= 0 || (size_t)f->width <= chars) { cb_fb_put(b, s, n); return; }
    size_t pad = (size_t)f->width - chars;
    if (numeric && f->zero) {
        cb_fb_put(b, s, prefix); cb_fb_fill(b, '0', pad); cb_fb_put(b, s + prefix, n - prefix);
        return;
    }
    char fill = f->fill ? f->fill : ' ';
    char al = f->align ? f->align : (numeric ? '>' : '<');
    size_t left = al == '>' ? pad : al == '^' ? pad / 2 : 0;
    cb_fb_fill(b, fill, left); cb_fb_put(b, s, n); cb_fb_fill(b, fill, pad - left);
}
/* insert `sep` every `every` digits, counted from the right, into digits[0..n) */
static size_t cb_fgroup(char *out, const char *d, size_t n, char sep, size_t every) {
    size_t o = 0;
    for (size_t i = 0; i < n; i++) {
        if (i && (n - i) % every == 0) out[o++] = sep;
        out[o++] = d[i];
    }
    return o;
}
static void cb_fmt_float(cb_fb *b, double v, int is32, const cb_fspec *f);
static void cb_fmt_int(cb_fb *b, long long sv, int is_signed, const cb_fspec *f) {
    char ty = f->type;
    if (ty == 'f' || ty == 'F' || ty == 'e' || ty == 'E' || ty == 'g' || ty == 'G') {
        cb_fmt_float(b, is_signed ? (double)sv : (double)(unsigned long long)sv, 0, f);
        return;
    }
    int neg = is_signed && sv < 0;
    unsigned long long u = neg ? 0ULL - (unsigned long long)sv : (unsigned long long)sv;
    if (ty == 'c') {
        char o[8]; size_t n = 0;
        unsigned long long c = (unsigned long long)sv;
        if (c < 0x80) o[n++] = (char)c;
        else if (c < 0x800) { o[n++] = (char)(0xC0 | (c >> 6)); o[n++] = (char)(0x80 | (c & 63)); }
        else if (c < 0x10000) { o[n++] = (char)(0xE0 | (c >> 12)); o[n++] = (char)(0x80 | ((c >> 6) & 63)); o[n++] = (char)(0x80 | (c & 63)); }
        else { c &= 0x1FFFFF; o[n++] = (char)(0xF0 | (c >> 18)); o[n++] = (char)(0x80 | ((c >> 12) & 63)); o[n++] = (char)(0x80 | ((c >> 6) & 63)); o[n++] = (char)(0x80 | (c & 63)); }
        cb_fpad(b, o, n, f, 0, 0);
        return;
    }
    unsigned base = ty == 'x' || ty == 'X' ? 16 : ty == 'b' ? 2 : ty == 'o' ? 8 : 10;
    const char *digs = ty == 'X' ? "0123456789ABCDEF" : "0123456789abcdef";
    char raw[72]; size_t rn = 0;
    do { raw[rn++] = digs[u % base]; u /= base; } while (u);
    char dd[72];
    for (size_t i = 0; i < rn; i++) dd[i] = raw[rn - 1 - i];
    char grouped[160]; size_t gn = rn;
    if (f->group) gn = cb_fgroup(grouped, dd, rn, f->group == 1 ? ',' : '_', base == 10 ? 3 : 4);
    char out[200]; size_t o = 0;
    if (neg) out[o++] = '-'; else if (f->sign) out[o++] = f->sign;
    if (f->alt && base != 10) { out[o++] = '0'; out[o++] = ty == 'X' ? 'X' : ty == 'b' ? 'b' : ty == 'o' ? 'o' : 'x'; }
    size_t prefix = o;
    memcpy(out + o, f->group ? grouped : dd, gn); o += gn;
    cb_fpad(b, out, o, f, 1, prefix);
}
static void cb_fmt_float(cb_fb *b, double v, int is32, const cb_fspec *f) {
    char num[1200], out[1400];
    char ty = f->type;
    if (v != v) snprintf(num, sizeof num, "nan");
    else if (isinf(v)) snprintf(num, sizeof num, v < 0 ? "-inf" : "inf");
    else if (!ty && f->prec < 0) cb_fmt_shortest(num, sizeof num, v, is32);
    else {
        int prec = f->prec < 0 ? 6 : f->prec;
        char fm[8] = "%.*f";
        fm[3] = ty ? ty : 'f';
        snprintf(num, sizeof num, fm, prec, v);
    }
    size_t o = 0; const char *s = num;
    if (*s == '-') { out[o++] = '-'; s++; }
    else if (f->sign) out[o++] = f->sign;
    size_t prefix = o;
    size_t il = 0;
    while (s[il] >= '0' && s[il] <= '9') il++;
    if (f->group && il > 0) o += cb_fgroup(out + o, s, il, f->group == 1 ? ',' : '_', 3);
    else { memcpy(out + o, s, il); o += il; }
    size_t rest = strlen(s + il);
    memcpy(out + o, s + il, rest); o += rest;
    cb_fpad(b, out, o, f, v == v && !isinf(v), prefix);
}
static void cb_fb_q(cb_fb *b, cb_str s) {            /* "text" with escapes */
    cb_fb_put(b, "\"", 1);
    for (int64_t i = 0; i < s.len; i++) {
        uint8_t c = s.ptr[i];
        if (c == '"') cb_fb_cstr(b, "\\\"");
        else if (c == '\\') cb_fb_cstr(b, "\\\\");
        else if (c == '\n') cb_fb_cstr(b, "\\n");
        else if (c == '\t') cb_fb_cstr(b, "\\t");
        else if (c == '\r') cb_fb_cstr(b, "\\r");
        else if (c < 32 || c == 127) { char t[8]; snprintf(t, sizeof t, "\\x%02x", c); cb_fb_cstr(b, t); }
        else cb_fb_put(b, &c, 1);
    }
    cb_fb_put(b, "\"", 1);
}
static void cb_fmt_str(cb_fb *b, cb_str s, const cb_fspec *f) {
    if (f->type == '?') {
        cb_fb t; cb_fb_init(&t); cb_fb_q(&t, s);
        cb_fpad(b, t.p, t.len, f, 0, 0);
        cb_fb_free(&t);
        return;
    }
    size_t n = (size_t)s.len;
    if (f->prec >= 0) {                                /* at most prec characters */
        size_t chars = 0, i = 0;
        while (i < n && chars < (size_t)f->prec) { i++; while (i < n && ((s.ptr[i] & 0xC0) == 0x80)) i++; chars++; }
        n = i;
    }
    cb_fpad(b, (const char *)s.ptr, n, f, 0, 0);
}
static void cb_fmt_ptr(cb_fb *b, const void *p, const cb_fspec *f) {
    char t[32]; snprintf(t, sizeof t, "%p", p);
    cb_fpad(b, t, strlen(t), f, 0, 0);
}
/* the automatic `{:?}` pieces */
static void cb_fb_i(cb_fb *b, long long v) { char t[32]; snprintf(t, sizeof t, "%lld", v); cb_fb_cstr(b, t); }
static void cb_fb_u(cb_fb *b, unsigned long long v) { char t[32]; snprintf(t, sizeof t, "%llu", v); cb_fb_cstr(b, t); }
static void cb_fb_f(cb_fb *b, double v, int is32) {
    char t[64];
    if (v != v) cb_fb_cstr(b, "nan");
    else if (isinf(v)) cb_fb_cstr(b, v < 0 ? "-inf" : "inf");
    else { cb_fmt_shortest(t, sizeof t, v, is32); cb_fb_cstr(b, t); }
}
static void cb_fb_ptr(cb_fb *b, const void *p) { char t[32]; snprintf(t, sizeof t, "%p", p); cb_fb_cstr(b, t); }
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

/* main's argument vector, read by std.env through FFI */
static int cb_argc_v;
static char **cb_argv_v;

#ifndef CB_NOSTD
/* ---- std.own: blocks that have exactly one owner, with a generation counter -------------------------
   Every block is a 16-byte header (generation, capacity) followed by the payload; the language only sees the payload.
   The generation is odd while the block is live and even while it is free, and goes up by one every time the state
   changes, so a `Ref` that remembers an old generation can tell that its block was freed (or freed and reused).
   Freed blocks are never returned to the system: they wait on a per-size free list, so a stale reference always reads a
   valid header. With the collector the blocks are ordinary collected blocks (nothing is recycled; the collector reclaims
   a block once the owner is gone and no `Ref` points at it). */
#include <sys/mman.h>
typedef struct { uint64_t gen; uint64_t cap; } cb_OwnHdr;
#define CB_OWN_BIG 65536                                      /* blocks this large give their pages back to the system when freed */
#ifndef CB_GC
static __thread void *cb_own_free_list[64];                 /* payload pointers, one list per power-of-two capacity; the next link is stored in the payload */
#endif
void *cb_own_alloc(int64_t size) {
    if (size < 16) size = 16;
    int cls = 64 - __builtin_clzll((uint64_t)size - 1);
    size_t cap = (size_t)1 << cls;
#ifdef CB_GC
    cb_OwnHdr *h = (cb_OwnHdr *)cb_mem_alloc(sizeof *h + cap, 0);
    h->gen = 1; h->cap = cap;
    return h + 1;
#else
    void *p = cb_own_free_list[cls];
    if (p) {
        cb_own_free_list[cls] = *(void **)p;
        if (cap >= CB_OWN_BIG) {                              /* the pages were handed back when it was freed: they are already zero */
            uintptr_t a = ((uintptr_t)p + sizeof(void *) + 4095) & ~(uintptr_t)4095;
            memset(p, 0, a > (uintptr_t)p + cap ? cap : (size_t)(a - (uintptr_t)p));
        } else memset(p, 0, cap);
        ((cb_OwnHdr *)p - 1)->gen++;                          /* even -> odd: live again, under a new generation */
        return p;
    }
    cb_OwnHdr *h = (cb_OwnHdr *)calloc(1, sizeof *h + cap);
    if (!h) { fputs("panic: out of memory (own)\n", stderr); exit(101); }
    h->gen = 1; h->cap = cap;
    return h + 1;
#endif
}
void cb_own_free(void *p) {
    if (!p) return;
    cb_OwnHdr *h = (cb_OwnHdr *)p - 1;
    if (!(h->gen & 1)) { fflush(stdout); fputs("panic: value freed twice\n", stderr); exit(101); }
    h->gen++;                                                 /* odd -> even: every Ref to this block is now stale */
#ifdef CB_GC
    memset(p, 0, h->cap);                                     /* do not keep what the dead value pointed to alive */
#else
    if (h->cap >= CB_OWN_BIG) {
        /* big block: give its whole pages back to the system now. The header and the first page stay mapped, so a stale Ref
           still reads a valid (odd/even) generation; the returned pages read as zeros until the block is reused. */
        uintptr_t a = ((uintptr_t)p + sizeof(void *) + 4095) & ~(uintptr_t)4095;
        uintptr_t b = ((uintptr_t)p + h->cap) & ~(uintptr_t)4095;
        if (b > a) madvise((void *)a, b - a, MADV_DONTNEED);
    } else memset(p, 0xDD, h->cap);                           /* a stale raw pointer reads 0xDD.. instead of old data */
    int cls = 63 - __builtin_clzll(h->cap);
    *(void **)p = cb_own_free_list[cls];
    cb_own_free_list[cls] = p;
#endif
}
uint64_t cb_own_gen(void *p) { return p ? ((cb_OwnHdr *)p - 1)->gen : 0; }
/* atomic counters for std.own's Rc/Weak (the pointer is an i64 inside a heap block) */
int64_t cb_atomic_add(int64_t *p, int64_t n) { return __atomic_fetch_add(p, n, __ATOMIC_ACQ_REL); }
int64_t cb_atomic_load(int64_t *p) { return __atomic_load_n(p, __ATOMIC_ACQUIRE); }
/* raises *p by one unless it is zero; true when it did (a Weak turning into an Rc) */
int cb_atomic_inc_nonzero(int64_t *p) {
    int64_t n = __atomic_load_n(p, __ATOMIC_ACQUIRE);
    while (n != 0) {
        if (__atomic_compare_exchange_n(p, &n, n + 1, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) return 1;
    }
    return 0;
}
#endif
/* ---- end runtime ----------------------------------------------------- */
