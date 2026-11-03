fn fib(n: i32) -> i64 { if n < 2 { n as i64 } else { fib(n - 1) + fib(n - 2) } }
fn main() { println!("{}", fib(40)); }
