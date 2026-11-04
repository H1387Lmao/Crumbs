#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
int main(void) {
    int64_t n = 50000000;
    char *composite = calloc(n + 1, 1);
    int64_t count = 0;
    for (int64_t i = 2; i <= n; i++) {
        if (!composite[i]) {
            count++;
            for (int64_t j = i * i; j <= n; j += i) composite[j] = 1;
        }
    }
    printf("%lld\n", (long long)count);
    free(composite);
    return 0;
}
