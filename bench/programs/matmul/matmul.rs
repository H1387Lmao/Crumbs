fn main() {
    let n: usize = 600;
    let mut a = vec![0.0f64; n * n];
    let mut b = vec![0.0f64; n * n];
    let mut c = vec![0.0f64; n * n];
    for i in 0..n { for j in 0..n { a[i * n + j] = ((i + j) % 10) as f64; b[i * n + j] = ((i * 3 + j) % 10) as f64; } }
    for i in 0..n { for k in 0..n { let aik = a[i * n + k]; for j in 0..n { c[i * n + j] += aik * b[k * n + j]; } } }
    let mut trace = 0.0; for i in 0..n { trace += c[i * n + i]; }
    println!("{}", trace as i64);
}
