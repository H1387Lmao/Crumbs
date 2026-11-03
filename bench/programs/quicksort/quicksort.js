function qsort(a, lo, hi) {
  if (lo >= hi) return;
  const mid = lo + Math.floor((hi - lo) / 2);
  let t = a[mid]; a[mid] = a[hi]; a[hi] = t;
  const pivot = a[hi]; let i = lo;
  for (let j = lo; j < hi; j++) if (a[j] < pivot) { const u = a[i]; a[i] = a[j]; a[j] = u; i++; }
  const v = a[i]; a[i] = a[hi]; a[hi] = v;
  qsort(a, lo, i - 1); qsort(a, i + 1, hi);
}
const n = 3000000;
const a = new Float64Array(n);
let x = 12345;
for (let i = 0; i < n; i++) { x = (Math.imul(x, 1103515245) + 12345) & 0x7fffffff; a[i] = x; }
qsort(a, 0, n - 1);
let sum = 0, ok = true;
for (let i = 0; i < n; i++) sum += (a[i] % 1000) * (i % 13);
for (let i = 1; i < n; i++) if (a[i - 1] > a[i]) ok = false;
console.log(sum + " " + ok);
