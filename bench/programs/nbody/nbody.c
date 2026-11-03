#include <stdio.h>
#include <math.h>
typedef struct { double x, y, z, vx, vy, vz, mass; } Body;
static double energy(Body *b) {
    double e = 0.0;
    for (int i = 0; i < 5; i++) {
        e += 0.5 * b[i].mass * (b[i].vx * b[i].vx + b[i].vy * b[i].vy + b[i].vz * b[i].vz);
        for (int j = i + 1; j < 5; j++) {
            double dx = b[i].x - b[j].x, dy = b[i].y - b[j].y, dz = b[i].z - b[j].z;
            e -= b[i].mass * b[j].mass / sqrt(dx * dx + dy * dy + dz * dz);
        }
    }
    return e;
}
static void advance(Body *b, double dt) {
    for (int i = 0; i < 5; i++) {
        for (int j = i + 1; j < 5; j++) {
            double dx = b[i].x - b[j].x, dy = b[i].y - b[j].y, dz = b[i].z - b[j].z;
            double d2 = dx * dx + dy * dy + dz * dz;
            double mag = dt / (d2 * sqrt(d2));
            b[i].vx -= dx * b[j].mass * mag; b[i].vy -= dy * b[j].mass * mag; b[i].vz -= dz * b[j].mass * mag;
            b[j].vx += dx * b[i].mass * mag; b[j].vy += dy * b[i].mass * mag; b[j].vz += dz * b[i].mass * mag;
        }
    }
    for (int i = 0; i < 5; i++) { b[i].x += dt * b[i].vx; b[i].y += dt * b[i].vy; b[i].z += dt * b[i].vz; }
}
int main(void) {
    double pi = 3.141592653589793, solar = 4.0 * pi * pi, dpy = 365.24;
    Body b[5] = {
        {0, 0, 0, 0, 0, 0, solar},
        {4.84143144246472090, -1.16032004402742839, -0.103622044471123109,
         0.00166007664274403694 * dpy, 0.00769901118419740425 * dpy, -0.0000690460016972063023 * dpy, 0.000954791938424326609 * solar},
        {8.34336671824457987, 4.12479856412430479, -0.403523417114321381,
         -0.00276742510726862411 * dpy, 0.00499852801234917238 * dpy, 0.0000230417297573763929 * dpy, 0.000285885980666130812 * solar},
        {12.8943695621391310, -15.1111514016986312, -0.223307578892655734,
         0.00296460137564761618 * dpy, 0.00237847173959480950 * dpy, -0.0000296589568540237556 * dpy, 0.0000436624404335156298 * solar},
        {15.3796971148509165, -25.9193146099879641, 0.179258772950371181,
         0.00268067772490389322 * dpy, 0.00162824170038242295 * dpy, -0.0000951592254519715870 * dpy, 0.0000515138902046611451 * solar}};
    double px = 0, py = 0, pz = 0;
    for (int i = 0; i < 5; i++) { px += b[i].vx * b[i].mass; py += b[i].vy * b[i].mass; pz += b[i].vz * b[i].mass; }
    b[0].vx = -px / solar; b[0].vy = -py / solar; b[0].vz = -pz / solar;
    printf("%.9f\n", energy(b));
    for (int i = 0; i < 5000000; i++) advance(b, 0.01);
    printf("%.9f\n", energy(b));
    return 0;
}
