# Benchmarks

Machine: Intel(R) Xeon(R) Processor @ 2.10GHz, 2 cores. Best of 5 runs, wall-clock seconds (Java and Node include their start-up). Lower is better; the figure in brackets is the time relative to C.

| program | Crumbs | Crumbs (--no-gc) | Crumbs (unchecked) | C (gcc -O2) | C++ (g++ -O2) | Rust (-O) | Go | Java | Node.js |
|---|---|---|---|---|---|---|---|---|---|
| fib(40): recursive calls | 0.17 (1.1x) | 0.16 (1.0x) | 0.19 (1.2x) | 0.16 (1.0x) | 0.16 (1.0x) | 0.29 (1.8x) | 0.50 (3.2x) | 0.52 (3.3x) | 0.96 (6.2x) |
| Collatz chains below 3M: integer loops | 0.77 (1.0x) | 0.79 (1.0x) | 0.74 (0.9x) | 0.79 (1.0x) | 0.81 (1.0x) | 0.54 (0.7x) | 0.82 (1.0x) | 0.97 (1.2x) | 5.03 (6.4x) |
| Mandelbrot 2000x2000: float loops | 0.26 (1.0x) | 0.26 (1.0x) | 0.26 (1.0x) | 0.26 (1.0x) | 0.26 (1.0x) | 0.25 (1.0x) | 0.26 (1.0x) | 0.36 (1.4x) | 0.30 (1.2x) |
| N-body, 5M steps: float math on structs | 0.24 (1.0x) | 0.24 (1.0x) | 0.25 (1.0x) | 0.24 (1.0x) | 0.25 (1.0x) | 0.19 (0.8x) | 0.31 (1.3x) | 0.34 (1.4x) | 0.45 (1.9x) |
| Matrix multiply 600x600: memory and float | 0.11 (1.4x) | 0.08 (0.9x) | 0.09 (1.1x) | 0.08 (1.0x) | 0.07 (0.8x) | 0.07 (0.9x) | 0.16 (1.9x) | 0.15 (1.8x) | 0.35 (4.3x) |
| Sieve of Eratosthenes, 50M: byte array | 0.29 (1.0x) | 0.31 (1.1x) | 0.30 (1.1x) | 0.28 (1.0x) | 0.27 (1.0x) | 0.27 (1.0x) | 0.29 (1.0x) | 0.31 (1.1x) | 0.41 (1.5x) |
| Quicksort, 3M integers: array swaps | 0.32 (1.1x) | 0.32 (1.1x) | 0.29 (1.0x) | 0.28 (1.0x) | 0.28 (1.0x) | 0.29 (1.0x) | 0.30 (1.1x) | 0.42 (1.5x) | 0.53 (1.9x) |
| Binary trees, depth 16: allocation | 2.22 (10.5x) | 0.26 (1.2x) | 1.92 (9.1x) | 0.21 (1.0x) | 0.27 (1.3x) | 0.29 (1.4x) | 0.52 (2.5x) | 0.25 (1.2x) | 0.24 (1.1x) |
| Hash map, 1M string keys: library code | 2.66 | 0.97 | 2.54 | n/a | 1.34 | 0.83 | 0.97 | 0.58 | 1.43 |

## Peak memory (MiB)

| program | Crumbs | Crumbs (--no-gc) | Crumbs (unchecked) | C (gcc -O2) | C++ (g++ -O2) | Rust (-O) | Go | Java | Node.js |
|---|---|---|---|---|---|---|---|---|---|
| fib(40): recursive calls | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 39 | 47 |
| Collatz chains below 3M: integer loops | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 42 | 48 |
| Mandelbrot 2000x2000: float loops | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 40 | 49 |
| N-body, 5M steps: float math on structs | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 42 | 50 |
| Matrix multiply 600x600: memory and float | 10 | 10 | 10 | 10 | 12 | 10 | 10 | 49 | 58 |
| Sieve of Eratosthenes, 50M: byte array | 49 | 49 | 49 | 49 | 52 | 50 | 50 | 88 | 95 |
| Quicksort, 3M integers: array swaps | 25 | 25 | 25 | 25 | 25 | 25 | 25 | 65 | 73 |
| Binary trees, depth 16: allocation | 108 | 9 | 105 | 10 | 12 | 10 | 15 | 277 | 93 |
| Hash map, 1M string keys: library code | 162 | 133 | 163 | n/a | 76 | 134 | 103 | 256 | 145 |

## Compile time and binary size

| language | total compile time (s) | binary size, mean (KiB) |
|---|---|---|
| Crumbs | 4.3 | 28 |
| Crumbs (--no-gc) | 2.4 | 19 |
| Crumbs (unchecked) | 4.3 | 28 |
| C (gcc -O2) | 0.5 | 16 |
| C++ (g++ -O2) | 2.4 | 17 |
| Rust (-O) | 9.4 | 4264 |
| Go | 2.8 | 2164 |
| Java | 5.7 | n/a |
| Node.js | 0.0 | n/a |

## Toolchains

- gcc: gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
- g++: g++ (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
- rustc: rustc 1.97.0 (2d8144b78 2026-07-07)
- go: go version go1.24.7 linux/amd64
- node: v22.23.4
- java: openjdk version "21.0.12.1" 2026-08-18
