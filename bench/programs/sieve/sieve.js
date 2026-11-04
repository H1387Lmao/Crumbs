const n = 50000000;
const composite = new Uint8Array(n + 1);
let count = 0;
for (let i = 2; i <= n; i++) {
  if (!composite[i]) {
    count++;
    for (let j = i * i; j <= n; j += i) composite[j] = 1;
  }
}
console.log(String(count));
