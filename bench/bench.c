#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
static int64_t fib(int n){ return n<2? n : fib(n-1)+fib(n-2); }
static int64_t sieve(int64_t n){ char*f=calloc(n+1,1); int64_t c=0; for(int64_t i=2;i<=n;i++) if(!f[i]){c++; for(int64_t j=i*i;j<=n;j+=i) f[j]=1;} free(f); return c; }
static int64_t mix(int n){ int64_t a=0; for(int i=0;i<n;i++) for(int j=0;j<n;j++) a+=(i*j)%7; return a; }
int main(){ printf("fib(40)      = %lld\n",(long long)fib(40)); printf("primes<=5e7  = %lld\n",(long long)sieve(50000000)); printf("mix(8000)    = %lld\n",(long long)mix(8000)); }
