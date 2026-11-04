package main

import "fmt"

func fib(n int32) int64 {
	if n < 2 {
		return int64(n)
	}
	return fib(n-1) + fib(n-2)
}
func main() { fmt.Println(fib(40)) }
