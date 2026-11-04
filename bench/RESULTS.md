# Benchmarks

Machine: Intel(R) Xeon(R) Processor @ 2.10GHz, 2 cores. Best of 5 runs, wall-clock seconds (Java and Node include their start-up). Lower is better; the figure in brackets is the time relative to C.

| program | Crumbs (owned memory) | Crumbs (--gc) | Crumbs (unchecked) | C (gcc -O2) | C++ (g++ -O2) | Rust (-O) | Go | Java | Node.js |
|---|---|---|---|---|---|---|---|---|---|
| fib(40): recursive calls | 0.18 (1.0x) | 0.18 (1.0x) | 0.20 (1.2x) | 0.17 (1.0x) | 0.18 (1.0x) | 0.31 (1.8x) | 0.54 (3.2x) | 0.57 (3.3x) | 1.04 (6.1x) |
| Collatz chains below 3M: integer loops | 0.85 (1.0x) | 0.90 (1.1x) | 0.83 (1.0x) | 0.83 (1.0x) | 0.84 (1.0x) | 0.58 (0.7x) | 0.90 (1.1x) | 1.03 (1.2x) | 5.73 (6.9x) |
| Mandelbrot 2000x2000: float loops | 0.29 (1.0x) | 0.28 (1.0x) | 0.29 (1.0x) | 0.28 (1.0x) | 0.28 (1.0x) | 0.27 (1.0x) | 0.28 (1.0x) | 0.39 (1.4x) | 0.32 (1.1x) |
| N-body, 5M steps: float math on structs | 0.26 (1.0x) | 0.26 (1.0x) | 0.27 (1.1x) | 0.26 (1.0x) | 0.25 (1.0x) | 0.21 (0.8x) | 0.35 (1.4x) | 0.40 (1.6x) | 0.51 (2.0x) |
| Matrix multiply 600x600: memory and float | 0.09 (1.1x) | 0.13 (1.6x) | 0.07 (0.9x) | 0.08 (1.0x) | 0.08 (1.0x) | 0.08 (0.9x) | 0.18 (2.2x) | 0.17 (2.0x) | 0.38 (4.7x) |
| Sieve of Eratosthenes, 50M: byte array | 0.30 (1.0x) | 0.30 (1.0x) | 0.31 (1.0x) | 0.30 (1.0x) | 0.30 (1.0x) | 0.29 (1.0x) | 0.29 (1.0x) | 0.36 (1.2x) | 0.47 (1.6x) |
| Quicksort, 3M integers: array swaps | 0.35 (1.1x) | 0.34 (1.1x) | 0.31 (1.0x) | 0.31 (1.0x) | 0.30 (1.0x) | 0.29 (0.9x) | 0.33 (1.1x) | 0.45 (1.5x) | 0.55 (1.8x) |
| Binary trees, depth 16: allocation | 0.26 (1.1x) | 2.04 (8.9x) | 0.25 (1.1x) | 0.23 (1.0x) | 0.30 (1.3x) | 0.31 (1.3x) | 0.57 (2.5x) | 0.27 (1.2x) | 0.25 (1.1x) |
| Hash map, 1M string keys: library code | 1.03 | 2.46 | 1.00 | n/a | 1.36 | 0.86 | 1.03 | 0.61 | 1.56 |

## Peak memory (MiB)

| program | Crumbs (owned memory) | Crumbs (--gc) | Crumbs (unchecked) | C (gcc -O2) | C++ (g++ -O2) | Rust (-O) | Go | Java | Node.js |
|---|---|---|---|---|---|---|---|---|---|
| fib(40): recursive calls | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 39 | 47 |
| Collatz chains below 3M: integer loops | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 42 | 48 |
| Mandelbrot 2000x2000: float loops | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 40 | 49 |
| N-body, 5M steps: float math on structs | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 43 | 50 |
| Matrix multiply 600x600: memory and float | 10 | 10 | 10 | 10 | 12 | 10 | 10 | 49 | 58 |
| Sieve of Eratosthenes, 50M: byte array | 49 | 49 | 49 | 49 | 52 | 50 | 50 | 88 | 95 |
| Quicksort, 3M integers: array swaps | 25 | 25 | 25 | 25 | 25 | 25 | 25 | 65 | 73 |
| Binary trees, depth 16: allocation | 14 | 107 | 14 | 10 | 12 | 10 | 14 | 277 | 93 |
| Hash map, 1M string keys: library code | 133 | 162 | 133 | n/a | 76 | 134 | 100 | 251 | 145 |

## Compile time and binary size

| language | total compile time (s) | binary size, mean (KiB) |
|---|---|---|
| Crumbs (owned memory) | 3.4 | 22 |
| Crumbs (--gc) | 4.6 | 28 |
| Crumbs (unchecked) | 3.2 | 23 |
| C (gcc -O2) | 0.6 | 16 |
| C++ (g++ -O2) | 2.5 | 17 |
| Rust (-O) | 9.3 | 4264 |
| Go | 2.9 | 2164 |
| Java | 6.4 | n/a |
| Node.js | 0.0 | n/a |

## Toolchains

- gcc: gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
- g++: g++ (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
- rustc: rustc 1.97.0 (2d8144b78 2026-07-07)
- go: go version go1.24.7 linux/amd64
- node: v22.23.4
- java: openjdk version "21.0.12.1" 2026-08-18
