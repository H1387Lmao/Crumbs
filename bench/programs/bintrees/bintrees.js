class Node { constructor(l, r) { this.l = l; this.r = r; } }
function make(d) { return d > 0 ? new Node(make(d - 1), make(d - 1)) : new Node(null, null); }
function check(t) { return t.l === null ? 1 : 1 + check(t.l) + check(t.r); }
const maxDepth = 16;
let total = check(make(maxDepth + 1));
const longLived = make(maxDepth);
for (let d = 4; d <= maxDepth; d += 2) {
  const iters = 1 << (maxDepth - d + 4);
  for (let i = 0; i < iters; i++) total += check(make(d));
}
total += check(longLived);
console.log(String(total));
