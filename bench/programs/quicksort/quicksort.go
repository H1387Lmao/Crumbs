package main

import "fmt"

func qsort(a []int64, lo, hi int64) {
	if lo >= hi {
		return
	}
	mid := lo + (hi-lo)/2
	a[mid], a[hi] = a[hi], a[mid]
	pivot := a[hi]
	i := lo
	for j := lo; j < hi; j++ {
		if a[j] < pivot {
			a[i], a[j] = a[j], a[i]
			i++
		}
	}
	a[i], a[hi] = a[hi], a[i]
	qsort(a, lo, i-1)
	qsort(a, i+1, hi)
}

func main() {
	var n int64 = 3000000
	a := make([]int64, n)
	var x int64 = 12345
	for i := int64(0); i < n; i++ {
		x = (x*1103515245 + 12345) % 2147483648
		a[i] = x
	}
	qsort(a, 0, n-1)
	var sum int64
	for i := int64(0); i < n; i++ {
		sum += (a[i] % 1000) * (i % 13)
	}
	ok := true
	for i := int64(1); i < n; i++ {
		if a[i-1] > a[i] {
			ok = false
		}
	}
	fmt.Println(sum, ok)
}
