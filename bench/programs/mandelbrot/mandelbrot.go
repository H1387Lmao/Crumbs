package main

import "fmt"

func main() {
	n := 2000
	var inside int64
	for py := 0; py < n; py++ {
		ci := 2.0*float64(py)/float64(n) - 1.0
		for px := 0; px < n; px++ {
			cr := 2.0*float64(px)/float64(n) - 1.5
			zr, zi, k := 0.0, 0.0, 0
			for k < 50 && zr*zr+zi*zi <= 4.0 {
				t := zr*zr - zi*zi + cr
				zi = 2.0*zr*zi + ci
				zr = t
				k++
			}
			if k == 50 {
				inside++
			}
		}
	}
	fmt.Println(inside)
}
