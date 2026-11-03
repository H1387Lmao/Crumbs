function fib(n) { return n < 2 ? n : fib(n - 1) + fib(n - 2); }
console.log(String(fib(40)));
