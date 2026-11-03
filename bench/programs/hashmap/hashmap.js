const n = 1000000;
const m = new Map();
for (let i = 0; i < n; i++) m.set("key" + i, i);
let sum = 0, missing = 0;
for (let i = 0; i < n; i++) { const v = m.get("key" + i); sum += v === undefined ? -1 : v; }
for (let i = 0; i < n; i++) if (!m.has("nokey" + i)) missing++;
console.log(m.size + " " + sum + " " + missing);
