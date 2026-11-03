public class Collatz {
    public static void main(String[] a) {
        long limit = 3000000, best = 0, bestN = 0;
        for (long n = 1; n < limit; n++) {
            long x = n, steps = 0;
            while (x != 1) { if (x % 2 == 0) x = x / 2; else x = 3 * x + 1; steps++; }
            if (steps > best) { best = steps; bestN = n; }
        }
        System.out.println(bestN + " " + best);
    }
}
