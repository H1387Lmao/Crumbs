#include <stdio.h>
int main(void) {
    int n = 2000; long long inside = 0;
    for (int py = 0; py < n; py++) {
        double ci = 2.0 * (double)py / (double)n - 1.0;
        for (int px = 0; px < n; px++) {
            double cr = 2.0 * (double)px / (double)n - 1.5, zr = 0.0, zi = 0.0;
            int k = 0;
            while (k < 50 && zr * zr + zi * zi <= 4.0) {
                double t = zr * zr - zi * zi + cr;
                zi = 2.0 * zr * zi + ci; zr = t; k++;
            }
            if (k == 50) inside++;
        }
    }
    printf("%lld\n", inside);
    return 0;
}
