use std::collections::HashMap;
fn main() {
    let n: i64 = 1_000_000;
    let mut m: HashMap<String, i64> = HashMap::new();
    for i in 0..n { m.insert(format!("key{}", i), i); }
    let (mut sum, mut missing) = (0i64, 0i64);
    for i in 0..n { sum += *m.get(&format!("key{}", i)).unwrap_or(&-1); }
    for i in 0..n { if !m.contains_key(&format!("nokey{}", i)) { missing += 1; } }
    println!("{} {} {}", m.len(), sum, missing);
}
