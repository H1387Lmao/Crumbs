const pi = 3.141592653589793, solar = 4 * pi * pi, dpy = 365.24;
const mk = (x, y, z, vx, vy, vz, m) => [x, y, z, vx * dpy, vy * dpy, vz * dpy, m * solar];
const b = [
  [0, 0, 0, 0, 0, 0, solar],
  mk(4.84143144246472090, -1.16032004402742839, -0.103622044471123109, 0.00166007664274403694, 0.00769901118419740425, -0.0000690460016972063023, 0.000954791938424326609),
  mk(8.34336671824457987, 4.12479856412430479, -0.403523417114321381, -0.00276742510726862411, 0.00499852801234917238, 0.0000230417297573763929, 0.000285885980666130812),
  mk(12.8943695621391310, -15.1111514016986312, -0.223307578892655734, 0.00296460137564761618, 0.00237847173959480950, -0.0000296589568540237556, 0.0000436624404335156298),
  mk(15.3796971148509165, -25.9193146099879641, 0.179258772950371181, 0.00268067772490389322, 0.00162824170038242295, -0.0000951592254519715870, 0.0000515138902046611451),
];
function energy() {
  let e = 0;
  for (let i = 0; i < 5; i++) {
    const bi = b[i];
    e += 0.5 * bi[6] * (bi[3] * bi[3] + bi[4] * bi[4] + bi[5] * bi[5]);
    for (let j = i + 1; j < 5; j++) {
      const bj = b[j], dx = bi[0] - bj[0], dy = bi[1] - bj[1], dz = bi[2] - bj[2];
      e -= bi[6] * bj[6] / Math.sqrt(dx * dx + dy * dy + dz * dz);
    }
  }
  return e;
}
function advance(dt) {
  for (let i = 0; i < 5; i++) {
    const bi = b[i];
    for (let j = i + 1; j < 5; j++) {
      const bj = b[j], dx = bi[0] - bj[0], dy = bi[1] - bj[1], dz = bi[2] - bj[2];
      const d2 = dx * dx + dy * dy + dz * dz, mag = dt / (d2 * Math.sqrt(d2));
      bi[3] -= dx * bj[6] * mag; bi[4] -= dy * bj[6] * mag; bi[5] -= dz * bj[6] * mag;
      bj[3] += dx * bi[6] * mag; bj[4] += dy * bi[6] * mag; bj[5] += dz * bi[6] * mag;
    }
  }
  for (let i = 0; i < 5; i++) { const bi = b[i]; bi[0] += dt * bi[3]; bi[1] += dt * bi[4]; bi[2] += dt * bi[5]; }
}
let px = 0, py = 0, pz = 0;
for (let i = 0; i < 5; i++) { px += b[i][3] * b[i][6]; py += b[i][4] * b[i][6]; pz += b[i][5] * b[i][6]; }
b[0][3] = -px / solar; b[0][4] = -py / solar; b[0][5] = -pz / solar;
console.log(energy().toFixed(9));
for (let i = 0; i < 5000000; i++) advance(0.01);
console.log(energy().toFixed(9));
