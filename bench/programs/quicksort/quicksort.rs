fn qsort(a: &mut [i64], lo: i64, hi: i64) {
    if lo >= hi { return; }
    let mid = lo + (hi - lo) / 2;
    a.swap(mid as usize, hi as usize);
    let pivot = a[hi as usize];
    let mut i = lo;
    for j in lo..hi {
        if a[j as usize] < pivot { a.swap(i as usize, j as usize); i += 1; }
    }
    a.swap(i as usize, hi as usize);
    qsort(a, lo, i - 1);
    qsort(a, i + 1, hi);
}
fn main() {
    let n: i64 = 3_000_000;
    let mut a = vec![0i64; n as usize];
    let mut x: i64 = 12345;
    for i in 0..n as usize { x = (x * 1103515245 + 12345) % 2147483648; a[i] = x; }
    qsort(&mut a, 0, n - 1);
    let mut sum: i64 = 0;
    for i in 0..n as usize { sum += (a[i] % 1000) * (i as i64 % 13); }
    let ok = a.windows(2).all(|w| w[0] <= w[1]);
    println!("{} {}", sum, ok);
}
