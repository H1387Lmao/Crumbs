# Benchmarks

Machine: Intel(R) Xeon(R) Processor @ 2.80GHz, 2 cores. Best of 5 runs, wall-clock seconds (Java and Node include their start-up). Lower is better; the figure in brackets is the time relative to C.

| program | Crumbs | Crumbs (unchecked) | C (gcc -O2) | C++ (g++ -O2) | Rust (-O) | Go | Java | Node.js |
|---|---|---|---|---|---|---|---|---|
| fib(40): recursive calls | 0.23 (0.9x) | 0.28 (1.0x) | 0.27 (1.0x) | 0.27 (1.0x) | 0.34 (1.3x) | 0.63 (2.4x) | 0.56 (2.1x) | 1.95 (7.3x) |
| Collatz chains below 3M: integer loops | 0.92 (1.1x) | 0.90 (1.1x) | 0.84 (1.0x) | 0.84 (1.0x) | 0.44 (0.5x) | 0.93 (1.1x) | 1.09 (1.3x) | 5.42 (6.4x) |
| Mandelbrot 2000x2000: float loops | 0.38 (1.0x) | 0.41 (1.1x) | 0.37 (1.0x) | 0.38 (1.0x) | 0.36 (1.0x) | 0.39 (1.1x) | 0.48 (1.3x) | 0.43 (1.1x) |
| N-body, 5M steps: float math on structs | 0.41 (1.1x) | 0.39 (1.0x) | 0.38 (1.0x) | 0.38 (1.0x) | 0.27 (0.7x) | 0.48 (1.3x) | 0.68 (1.8x) | 0.84 (2.2x) |
| Matrix multiply 600x600: memory and float | 0.15 (1.4x) | 0.13 (1.2x) | 0.11 (1.0x) | 0.10 (0.9x) | 0.10 (0.9x) | 0.18 (1.7x) | 0.21 (2.0x) | 0.52 (4.8x) |
| Sieve of Eratosthenes, 50M: byte array | 0.89 (1.0x) | 0.91 (1.0x) | 0.88 (1.0x) | 0.91 (1.0x) | 0.90 (1.0x) | 0.92 (1.0x) | 0.99 (1.1x) | 1.15 (1.3x) |
| Quicksort, 3M integers: array swaps | 0.38 (1.2x) | 0.32 (1.0x) | 0.31 (1.0x) | 0.33 (1.1x) | 0.36 (1.2x) | 0.35 (1.1x) | 0.51 (1.7x) | 0.67 (2.1x) |
| Binary trees, depth 16: allocation | 3.55 (9.1x) | 3.24 (8.3x) | 0.39 (1.0x) | 0.45 (1.2x) | 0.53 (1.4x) | 0.88 (2.3x) | 0.35 (0.9x) | 0.48 (1.2x) |
| Hash map, 1M string keys: library code | 4.32 | 3.78 | n/a | 2.04 | 1.68 | 1.76 | 1.01 | 2.28 |

## Peak memory (MiB)

| program | Crumbs | Crumbs (unchecked) | C (gcc -O2) | C++ (g++ -O2) | Rust (-O) | Go | Java | Node.js |
|---|---|---|---|---|---|---|---|---|
| fib(40): recursive calls | 2 | 2 | 2 | 2 | 2 | 2 | 39 | 47 |
| Collatz chains below 3M: integer loops | 2 | 2 | 2 | 2 | 2 | 2 | 42 | 48 |
| Mandelbrot 2000x2000: float loops | 2 | 2 | 2 | 2 | 2 | 2 | 40 | 49 |
| N-body, 5M steps: float math on structs | 2 | 2 | 2 | 2 | 2 | 2 | 43 | 51 |
| Matrix multiply 600x600: memory and float | 10 | 10 | 10 | 12 | 10 | 10 | 49 | 58 |
| Sieve of Eratosthenes, 50M: byte array | 49 | 49 | 49 | 51 | 50 | 50 | 88 | 95 |
| Quicksort, 3M integers: array swaps | 24 | 24 | 25 | 25 | 25 | 25 | 65 | 73 |
| Binary trees, depth 16: allocation | 108 | 105 | 10 | 12 | 10 | 14 | 276 | 93 |
| Hash map, 1M string keys: library code | 162 | 163 | n/a | 76 | 134 | 103 | 253 | 145 |

## Compile time and binary size

| language | total compile time (s) | binary size, mean (KiB) |
|---|---|---|
| Crumbs | 6.2 | 28 |
| Crumbs (unchecked) | 7.0 | 28 |
| C (gcc -O2) | 0.8 | 16 |
| C++ (g++ -O2) | 2.5 | 17 |
| Rust (-O) | 2.3 | 4264 |
| Go | 1.1 | 2164 |
| Java | 8.3 | n/a |
| Node.js | 0.0 | n/a |

## Toolchains

- gcc: gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
- g++: g++ (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
- rustc: rustc 1.97.0 (2d8144b78 2026-07-07)
- go: go version go1.24.7 linux/amd64
- node: v22.23.4
- java: openjdk version "21.0.12.1" 2026-08-18
