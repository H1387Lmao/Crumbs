#include <stdio.h>
#include <stdint.h>
static int64_t fib(int n) { return n < 2 ? n : fib(n - 1) + fib(n - 2); }
int main(void) { printf("%lld\n", (long long)fib(40)); return 0; }
