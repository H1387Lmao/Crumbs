public class Sieve {
    public static void main(String[] a) {
        long n = 50000000L;
        boolean[] composite = new boolean[(int) n + 1];
        long count = 0;
        for (long i = 2; i <= n; i++) {
            if (!composite[(int) i]) {
                count++;
                for (long j = i * i; j <= n; j += i) composite[(int) j] = true;
            }
        }
        System.out.println(count);
    }
}
