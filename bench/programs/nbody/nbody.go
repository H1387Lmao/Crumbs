package main

import (
	"fmt"
	"math"
)

type Body struct{ x, y, z, vx, vy, vz, mass float64 }

func energy(b *[5]Body) float64 {
	e := 0.0
	for i := 0; i < 5; i++ {
		e += 0.5 * b[i].mass * (b[i].vx*b[i].vx + b[i].vy*b[i].vy + b[i].vz*b[i].vz)
		for j := i + 1; j < 5; j++ {
			dx, dy, dz := b[i].x-b[j].x, b[i].y-b[j].y, b[i].z-b[j].z
			e -= b[i].mass * b[j].mass / math.Sqrt(dx*dx+dy*dy+dz*dz)
		}
	}
	return e
}

func advance(b *[5]Body, dt float64) {
	for i := 0; i < 5; i++ {
		for j := i + 1; j < 5; j++ {
			dx, dy, dz := b[i].x-b[j].x, b[i].y-b[j].y, b[i].z-b[j].z
			d2 := dx*dx + dy*dy + dz*dz
			mag := dt / (d2 * math.Sqrt(d2))
			mi, mj := b[i].mass, b[j].mass
			b[i].vx -= dx * mj * mag
			b[i].vy -= dy * mj * mag
			b[i].vz -= dz * mj * mag
			b[j].vx += dx * mi * mag
			b[j].vy += dy * mi * mag
			b[j].vz += dz * mi * mag
		}
	}
	for i := 0; i < 5; i++ {
		b[i].x += dt * b[i].vx
		b[i].y += dt * b[i].vy
		b[i].z += dt * b[i].vz
	}
}

func main() {
	pi := 3.141592653589793
	solar := 4.0 * pi * pi
	dpy := 365.24
	mk := func(x, y, z, vx, vy, vz, m float64) Body {
		return Body{x, y, z, vx * dpy, vy * dpy, vz * dpy, m * solar}
	}
	b := [5]Body{
		{0, 0, 0, 0, 0, 0, solar},
		mk(4.84143144246472090, -1.16032004402742839, -0.103622044471123109, 0.00166007664274403694, 0.00769901118419740425, -0.0000690460016972063023, 0.000954791938424326609),
		mk(8.34336671824457987, 4.12479856412430479, -0.403523417114321381, -0.00276742510726862411, 0.00499852801234917238, 0.0000230417297573763929, 0.000285885980666130812),
		mk(12.8943695621391310, -15.1111514016986312, -0.223307578892655734, 0.00296460137564761618, 0.00237847173959480950, -0.0000296589568540237556, 0.0000436624404335156298),
		mk(15.3796971148509165, -25.9193146099879641, 0.179258772950371181, 0.00268067772490389322, 0.00162824170038242295, -0.0000951592254519715870, 0.0000515138902046611451),
	}
	px, py, pz := 0.0, 0.0, 0.0
	for i := 0; i < 5; i++ {
		px += b[i].vx * b[i].mass
		py += b[i].vy * b[i].mass
		pz += b[i].vz * b[i].mass
	}
	b[0].vx, b[0].vy, b[0].vz = -px/solar, -py/solar, -pz/solar
	fmt.Printf("%.9f\n", energy(&b))
	for i := 0; i < 5000000; i++ {
		advance(&b, 0.01)
	}
	fmt.Printf("%.9f\n", energy(&b))
}
