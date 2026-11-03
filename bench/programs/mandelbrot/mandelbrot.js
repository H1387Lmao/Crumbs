const n = 2000;
let inside = 0;
for (let py = 0; py < n; py++) {
  const ci = 2.0 * py / n - 1.0;
  for (let px = 0; px < n; px++) {
    const cr = 2.0 * px / n - 1.5;
    let zr = 0, zi = 0, k = 0;
    while (k < 50 && zr * zr + zi * zi <= 4.0) {
      const t = zr * zr - zi * zi + cr;
      zi = 2.0 * zr * zi + ci; zr = t; k++;
    }
    if (k === 50) inside++;
  }
}
console.log(String(inside));
