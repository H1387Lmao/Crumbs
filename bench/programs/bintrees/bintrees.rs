struct Node { l: Option<Box<Node>>, r: Option<Box<Node>> }
fn make(d: i32) -> Box<Node> {
    if d > 0 { Box::new(Node { l: Some(make(d - 1)), r: Some(make(d - 1)) }) } else { Box::new(Node { l: None, r: None }) }
}
fn check(t: &Node) -> i64 {
    match (&t.l, &t.r) { (Some(l), Some(r)) => 1 + check(l) + check(r), _ => 1 }
}
fn main() {
    let max_depth = 16;
    let mut total: i64 = check(&make(max_depth + 1));
    let long_lived = make(max_depth);
    let mut d = 4;
    while d <= max_depth {
        let iters = 1i64 << (max_depth - d + 4);
        for _ in 0..iters { total += check(&make(d)); }
        d += 2;
    }
    total += check(&long_lived);
    println!("{}", total);
}
