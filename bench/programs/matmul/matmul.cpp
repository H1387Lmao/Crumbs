#include <cstdio>
#include <vector>
int main() {
    long n = 600;
    std::vector<double> a(n * n), b(n * n), c(n * n, 0.0);
    for (long i = 0; i < n; i++) for (long j = 0; j < n; j++) { a[i * n + j] = (double)((i + j) % 10); b[i * n + j] = (double)((i * 3 + j) % 10); }
    for (long i = 0; i < n; i++) for (long k = 0; k < n; k++) { double aik = a[i * n + k]; for (long j = 0; j < n; j++) c[i * n + j] += aik * b[k * n + j]; }
    double trace = 0; for (long i = 0; i < n; i++) trace += c[i * n + i];
    std::printf("%lld\n", (long long)trace);
}
