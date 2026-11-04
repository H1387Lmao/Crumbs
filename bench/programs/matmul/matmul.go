package main

import "fmt"

func main() {
	n := 600
	a := make([]float64, n*n)
	b := make([]float64, n*n)
	c := make([]float64, n*n)
	for i := 0; i < n; i++ {
		for j := 0; j < n; j++ {
			a[i*n+j] = float64((i + j) % 10)
			b[i*n+j] = float64((i*3 + j) % 10)
		}
	}
	for i := 0; i < n; i++ {
		for k := 0; k < n; k++ {
			aik := a[i*n+k]
			for j := 0; j < n; j++ {
				c[i*n+j] += aik * b[k*n+j]
			}
		}
	}
	trace := 0.0
	for i := 0; i < n; i++ {
		trace += c[i*n+i]
	}
	fmt.Println(int64(trace))
}
