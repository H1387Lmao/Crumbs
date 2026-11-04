# Benchmarks

Machine: Intel(R) Xeon(R) Processor @ 2.10GHz, 2 cores. Best of 5 runs, wall-clock seconds (Java and Node include their start-up). Lower is better; the figure in brackets is the time relative to C.

| program | Crumbs | Crumbs (--no-gc) | Crumbs (std.own) | Crumbs (unchecked) | C (gcc -O2) | C++ (g++ -O2) | Rust (-O) | Go | Java | Node.js |
|---|---|---|---|---|---|---|---|---|---|---|
| fib(40): recursive calls | 0.17 (1.1x) | 0.17 (1.1x) | n/a | 0.18 (1.2x) | 0.16 (1.0x) | 0.16 (1.0x) | 0.29 (1.9x) | 0.49 (3.2x) | 0.54 (3.5x) | 1.01 (6.5x) |
| Collatz chains below 3M: integer loops | 0.80 (1.1x) | 0.80 (1.1x) | n/a | 0.79 (1.0x) | 0.75 (1.0x) | 0.75 (1.0x) | 0.55 (0.7x) | 0.85 (1.1x) | 0.95 (1.3x) | 5.04 (6.7x) |
| Mandelbrot 2000x2000: float loops | 0.26 (1.0x) | 0.27 (1.0x) | n/a | 0.26 (0.9x) | 0.27 (1.0x) | 0.27 (1.0x) | 0.25 (0.9x) | 0.25 (0.9x) | 0.36 (1.3x) | 0.30 (1.1x) |
| N-body, 5M steps: float math on structs | 0.24 (1.0x) | 0.24 (1.0x) | n/a | 0.24 (1.0x) | 0.23 (1.0x) | 0.23 (1.0x) | 0.19 (0.8x) | 0.28 (1.2x) | 0.34 (1.5x) | 0.45 (1.9x) |
| Matrix multiply 600x600: memory and float | 0.12 (1.5x) | 0.09 (1.1x) | n/a | 0.09 (1.1x) | 0.08 (1.0x) | 0.08 (1.0x) | 0.08 (1.0x) | 0.15 (2.0x) | 0.15 (1.9x) | 0.35 (4.5x) |
| Sieve of Eratosthenes, 50M: byte array | 0.29 (1.1x) | 0.28 (1.1x) | n/a | 0.28 (1.1x) | 0.26 (1.0x) | 0.28 (1.1x) | 0.27 (1.0x) | 0.28 (1.1x) | 0.32 (1.3x) | 0.41 (1.6x) |
| Quicksort, 3M integers: array swaps | 0.32 (1.1x) | 0.30 (1.1x) | n/a | 0.28 (1.0x) | 0.28 (1.0x) | 0.28 (1.0x) | 0.28 (1.0x) | 0.30 (1.1x) | 0.38 (1.4x) | 0.50 (1.8x) |
| Binary trees, depth 16: allocation | 1.99 (8.9x) | 0.26 (1.2x) | 0.23 (1.0x) | 1.96 (8.7x) | 0.22 (1.0x) | 0.28 (1.2x) | 0.30 (1.3x) | 0.52 (2.3x) | 0.29 (1.3x) | 0.23 (1.0x) |
| Hash map, 1M string keys: library code | 2.50 | 1.03 | n/a | 2.65 | n/a | 1.32 | 0.82 | 1.00 | 0.56 | 1.42 |

## Peak memory (MiB)

| program | Crumbs | Crumbs (--no-gc) | Crumbs (std.own) | Crumbs (unchecked) | C (gcc -O2) | C++ (g++ -O2) | Rust (-O) | Go | Java | Node.js |
|---|---|---|---|---|---|---|---|---|---|---|
| fib(40): recursive calls | 2 | 2 | n/a | 2 | 2 | 2 | 2 | 2 | 39 | 47 |
| Collatz chains below 3M: integer loops | 2 | 2 | n/a | 2 | 2 | 2 | 2 | 2 | 42 | 47 |
| Mandelbrot 2000x2000: float loops | 2 | 2 | n/a | 2 | 2 | 2 | 2 | 2 | 40 | 49 |
| N-body, 5M steps: float math on structs | 2 | 2 | n/a | 2 | 2 | 2 | 2 | 2 | 42 | 51 |
| Matrix multiply 600x600: memory and float | 10 | 10 | n/a | 10 | 10 | 12 | 10 | 10 | 49 | 58 |
| Sieve of Eratosthenes, 50M: byte array | 49 | 49 | n/a | 49 | 49 | 51 | 50 | 50 | 88 | 95 |
| Quicksort, 3M integers: array swaps | 25 | 25 | n/a | 25 | 25 | 25 | 25 | 25 | 65 | 73 |
| Binary trees, depth 16: allocation | 108 | 9 | 14 | 105 | 10 | 12 | 10 | 15 | 276 | 93 |
| Hash map, 1M string keys: library code | 162 | 133 | n/a | 163 | n/a | 76 | 134 | 100 | 255 | 147 |

## Compile time and binary size

| language | total compile time (s) | binary size, mean (KiB) |
|---|---|---|
| Crumbs | 4.0 | 28 |
| Crumbs (--no-gc) | 2.6 | 22 |
| Crumbs (std.own) | 0.3 | 25 |
| Crumbs (unchecked) | 4.2 | 28 |
| C (gcc -O2) | 0.6 | 16 |
| C++ (g++ -O2) | 2.4 | 17 |
| Rust (-O) | 9.4 | 4264 |
| Go | 1.8 | 2164 |
| Java | 5.9 | n/a |
| Node.js | 0.0 | n/a |

## Toolchains

- gcc: gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
- g++: g++ (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
- rustc: rustc 1.97.0 (2d8144b78 2026-07-07)
- go: go version go1.24.7 linux/amd64
- node: v22.23.4
- java: openjdk version "21.0.12.1" 2026-08-18
