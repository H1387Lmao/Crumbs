package main

import (
	"fmt"
	"strconv"
)

func main() {
	n := 1000000
	m := make(map[string]int64)
	for i := 0; i < n; i++ {
		m["key"+strconv.Itoa(i)] = int64(i)
	}
	var sum, missing int64
	for i := 0; i < n; i++ {
		if v, ok := m["key"+strconv.Itoa(i)]; ok {
			sum += v
		} else {
			sum--
		}
	}
	for i := 0; i < n; i++ {
		if _, ok := m["nokey"+strconv.Itoa(i)]; !ok {
			missing++
		}
	}
	fmt.Println(len(m), sum, missing)
}
