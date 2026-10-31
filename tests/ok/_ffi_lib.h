#ifndef FFI_LIB_H
#define FFI_LIB_H
#include <stdint.h>
#define FFI_VERSION 7
#define FFI_FLAG_A 0x01
#define FFI_FLAG_B 0x02
#define FFI_PI 3.5
typedef struct { float x, y; } Vec2;
typedef struct { int32_t id; char name[8]; Vec2 pos; } Item;
typedef struct Counter Counter;
Counter *counter_new(int start);
void counter_add(Counter *c, int n);
int counter_get(const Counter *c);
void counter_free(Counter *c);
int counters_alive(void);
Vec2 vec2_add(Vec2 a, Vec2 b);
double vec2_len(Vec2 v);
int apply_twice(int (*f)(int), int x);
void each_int(const int *xs, int n, void (*cb)(int, void *), void *user);
int sum_ints(int count, ...);
const char *greeting(void);
int c_calls_tern(int x);
void fill_item(Item *it);
int name_len(const char *s);
#endif
