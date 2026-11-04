package main

import "fmt"

func main() {
	var n int64 = 50000000
	composite := make([]bool, n+1)
	var count int64
	for i := int64(2); i <= n; i++ {
		if !composite[i] {
			count++
			for j := i * i; j <= n; j += i {
				composite[j] = true
			}
		}
	}
	fmt.Println(count)
}
