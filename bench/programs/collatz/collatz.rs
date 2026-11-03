fn main() {
    let limit: i64 = 3_000_000;
    let (mut best, mut best_n) = (0i64, 0i64);
    for n in 1..limit {
        let mut x = n;
        let mut steps = 0i64;
        while x != 1 {
            if x % 2 == 0 { x /= 2; } else { x = 3 * x + 1; }
            steps += 1;
        }
        if steps > best { best = steps; best_n = n; }
    }
    println!("{} {}", best_n, best);
}
