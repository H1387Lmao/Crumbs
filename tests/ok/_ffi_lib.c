#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <math.h>
#include "_ffi_lib.h"
struct Counter { int n; };
static int alive;
Counter *counter_new(int start) { Counter *c = malloc(sizeof *c); c->n = start; alive++; return c; }
void counter_add(Counter *c, int n) { c->n += n; }
int counter_get(const Counter *c) { return c->n; }
void counter_free(Counter *c) { free(c); alive--; }
int counters_alive(void) { return alive; }
Vec2 vec2_add(Vec2 a, Vec2 b) { Vec2 r = { a.x + b.x, a.y + b.y }; return r; }
double vec2_len(Vec2 v) { return sqrt((double)v.x * v.x + (double)v.y * v.y); }
int apply_twice(int (*f)(int), int x) { return f(f(x)); }
void each_int(const int *xs, int n, void (*cb)(int, void *), void *user) { for (int i = 0; i < n; i++) cb(xs[i], user); }
int sum_ints(int count, ...) { va_list ap; va_start(ap, count); int s = 0; for (int i = 0; i < count; i++) s += va_arg(ap, int); va_end(ap); return s; }
const char *greeting(void) { return "hello from C"; }
extern int tern_double(int);
int c_calls_tern(int x) { return tern_double(x) + 1; }
void fill_item(Item *it) { it->id = 9; strcpy(it->name, "widget"); it->pos.x = 1.5f; it->pos.y = 2.5f; }
int name_len(const char *s) { return (int)strlen(s); }
