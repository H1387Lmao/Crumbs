package main

import "fmt"

type Node struct{ l, r *Node }

func make_(d int) *Node {
	n := &Node{}
	if d > 0 {
		n.l = make_(d - 1)
		n.r = make_(d - 1)
	}
	return n
}
func check(t *Node) int64 {
	if t.l == nil {
		return 1
	}
	return 1 + check(t.l) + check(t.r)
}
func main() {
	maxDepth := 16
	total := check(make_(maxDepth + 1))
	longLived := make_(maxDepth)
	for d := 4; d <= maxDepth; d += 2 {
		iters := 1 << (maxDepth - d + 4)
		for i := 0; i < iters; i++ {
			total += check(make_(d))
		}
	}
	total += check(longLived)
	fmt.Println(total)
}
