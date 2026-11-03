#include <cstdio>
#include <cstdint>
#include <vector>
int main() {
    int64_t n = 50000000;
    std::vector<char> composite(n + 1, 0);
    int64_t count = 0;
    for (int64_t i = 2; i <= n; i++) {
        if (!composite[i]) {
            count++;
            for (int64_t j = i * i; j <= n; j += i) composite[j] = 1;
        }
    }
    std::printf("%lld\n", (long long)count);
}
