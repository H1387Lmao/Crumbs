const limit = 3000000;
let best = 0, bestN = 0;
for (let n = 1; n < limit; n++) {
  let x = n, steps = 0;
  while (x !== 1) { if (x % 2 === 0) x = x / 2; else x = 3 * x + 1; steps++; }
  if (steps > best) { best = steps; bestN = n; }
}
console.log(bestN + " " + best);
