#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
static void qsort_(int64_t *a, int64_t lo, int64_t hi) {
    if (lo >= hi) return;
    int64_t mid = lo + (hi - lo) / 2, t = a[mid]; a[mid] = a[hi]; a[hi] = t;
    int64_t pivot = a[hi], i = lo;
    for (int64_t j = lo; j < hi; j++) if (a[j] < pivot) { int64_t u = a[i]; a[i] = a[j]; a[j] = u; i++; }
    int64_t v = a[i]; a[i] = a[hi]; a[hi] = v;
    qsort_(a, lo, i - 1); qsort_(a, i + 1, hi);
}
int main(void) {
    int64_t n = 3000000, x = 12345, sum = 0; int ok = 1;
    int64_t *a = malloc(n * 8);
    for (int64_t i = 0; i < n; i++) { x = (x * 1103515245 + 12345) % 2147483648LL; a[i] = x; }
    qsort_(a, 0, n - 1);
    for (int64_t i = 0; i < n; i++) sum += (a[i] % 1000) * (i % 13);
    for (int64_t i = 1; i < n; i++) if (a[i - 1] > a[i]) ok = 0;
    printf("%lld %s\n", (long long)sum, ok ? "true" : "false");
    return 0;
}
