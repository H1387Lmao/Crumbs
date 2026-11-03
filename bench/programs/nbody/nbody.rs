#[derive(Clone, Copy)]
struct Body { x: f64, y: f64, z: f64, vx: f64, vy: f64, vz: f64, mass: f64 }
fn energy(b: &[Body; 5]) -> f64 {
    let mut e = 0.0;
    for i in 0..5 {
        e += 0.5 * b[i].mass * (b[i].vx * b[i].vx + b[i].vy * b[i].vy + b[i].vz * b[i].vz);
        for j in (i + 1)..5 {
            let (dx, dy, dz) = (b[i].x - b[j].x, b[i].y - b[j].y, b[i].z - b[j].z);
            e -= b[i].mass * b[j].mass / (dx * dx + dy * dy + dz * dz).sqrt();
        }
    }
    e
}
fn advance(b: &mut [Body; 5], dt: f64) {
    for i in 0..5 {
        for j in (i + 1)..5 {
            let (dx, dy, dz) = (b[i].x - b[j].x, b[i].y - b[j].y, b[i].z - b[j].z);
            let d2 = dx * dx + dy * dy + dz * dz;
            let mag = dt / (d2 * d2.sqrt());
            let (mi, mj) = (b[i].mass, b[j].mass);
            b[i].vx -= dx * mj * mag; b[i].vy -= dy * mj * mag; b[i].vz -= dz * mj * mag;
            b[j].vx += dx * mi * mag; b[j].vy += dy * mi * mag; b[j].vz += dz * mi * mag;
        }
    }
    for i in 0..5 { b[i].x += dt * b[i].vx; b[i].y += dt * b[i].vy; b[i].z += dt * b[i].vz; }
}
fn main() {
    let pi = 3.141592653589793f64; let solar = 4.0 * pi * pi; let dpy = 365.24;
    let mk = |x, y, z, vx: f64, vy: f64, vz: f64, m: f64| Body { x, y, z, vx: vx * dpy, vy: vy * dpy, vz: vz * dpy, mass: m * solar };
    let mut b = [
        Body { x: 0.0, y: 0.0, z: 0.0, vx: 0.0, vy: 0.0, vz: 0.0, mass: solar },
        mk(4.84143144246472090, -1.16032004402742839, -0.103622044471123109, 0.00166007664274403694, 0.00769901118419740425, -0.0000690460016972063023, 0.000954791938424326609),
        mk(8.34336671824457987, 4.12479856412430479, -0.403523417114321381, -0.00276742510726862411, 0.00499852801234917238, 0.0000230417297573763929, 0.000285885980666130812),
        mk(12.8943695621391310, -15.1111514016986312, -0.223307578892655734, 0.00296460137564761618, 0.00237847173959480950, -0.0000296589568540237556, 0.0000436624404335156298),
        mk(15.3796971148509165, -25.9193146099879641, 0.179258772950371181, 0.00268067772490389322, 0.00162824170038242295, -0.0000951592254519715870, 0.0000515138902046611451),
    ];
    let (mut px, mut py, mut pz) = (0.0, 0.0, 0.0);
    for i in 0..5 { px += b[i].vx * b[i].mass; py += b[i].vy * b[i].mass; pz += b[i].vz * b[i].mass; }
    b[0].vx = -px / solar; b[0].vy = -py / solar; b[0].vz = -pz / solar;
    println!("{:.9}", energy(&b));
    for _ in 0..5_000_000 { advance(&mut b, 0.01); }
    println!("{:.9}", energy(&b));
}
