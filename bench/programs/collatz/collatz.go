package main

import "fmt"

func main() {
	var limit, best, bestN int64 = 3000000, 0, 0
	for n := int64(1); n < limit; n++ {
		x, steps := n, int64(0)
		for x != 1 {
			if x%2 == 0 {
				x = x / 2
			} else {
				x = 3*x + 1
			}
			steps++
		}
		if steps > best {
			best = steps
			bestN = n
		}
	}
	fmt.Println(bestN, best)
}
