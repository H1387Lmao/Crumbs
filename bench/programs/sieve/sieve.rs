fn main() {
    let n: i64 = 50_000_000;
    let mut composite = vec![false; (n + 1) as usize];
    let mut count: i64 = 0;
    let mut i: i64 = 2;
    while i <= n {
        if !composite[i as usize] {
            count += 1;
            let mut j = i * i;
            while j <= n { composite[j as usize] = true; j += i; }
        }
        i += 1;
    }
    println!("{}", count);
}
