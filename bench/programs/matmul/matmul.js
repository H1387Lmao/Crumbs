const n = 600;
const a = new Float64Array(n * n), b = new Float64Array(n * n), c = new Float64Array(n * n);
for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) { a[i * n + j] = (i + j) % 10; b[i * n + j] = (i * 3 + j) % 10; }
for (let i = 0; i < n; i++) for (let k = 0; k < n; k++) { const aik = a[i * n + k]; for (let j = 0; j < n; j++) c[i * n + j] += aik * b[k * n + j]; }
let trace = 0; for (let i = 0; i < n; i++) trace += c[i * n + i];
console.log(String(Math.trunc(trace)));
