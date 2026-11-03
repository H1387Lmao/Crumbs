#include <stdio.h>
#include <stdlib.h>
int main(void) {
    long n = 600;
    double *a = malloc(n * n * 8), *b = malloc(n * n * 8), *c = calloc(n * n, 8);
    for (long i = 0; i < n; i++) for (long j = 0; j < n; j++) { a[i * n + j] = (double)((i + j) % 10); b[i * n + j] = (double)((i * 3 + j) % 10); }
    for (long i = 0; i < n; i++) for (long k = 0; k < n; k++) { double aik = a[i * n + k]; for (long j = 0; j < n; j++) c[i * n + j] += aik * b[k * n + j]; }
    double trace = 0; for (long i = 0; i < n; i++) trace += c[i * n + i];
    printf("%lld\n", (long long)trace);
    return 0;
}
