public class Quicksort {
    static void qsort(long[] a, int lo, int hi) {
        if (lo >= hi) return;
        int mid = lo + (hi - lo) / 2;
        long t = a[mid]; a[mid] = a[hi]; a[hi] = t;
        long pivot = a[hi]; int i = lo;
        for (int j = lo; j < hi; j++) if (a[j] < pivot) { long u = a[i]; a[i] = a[j]; a[j] = u; i++; }
        long v = a[i]; a[i] = a[hi]; a[hi] = v;
        qsort(a, lo, i - 1); qsort(a, i + 1, hi);
    }
    public static void main(String[] args) {
        int n = 3000000; long x = 12345, sum = 0; boolean ok = true;
        long[] a = new long[n];
        for (int i = 0; i < n; i++) { x = (x * 1103515245L + 12345) % 2147483648L; a[i] = x; }
        qsort(a, 0, n - 1);
        for (int i = 0; i < n; i++) sum += (a[i] % 1000) * (i % 13);
        for (int i = 1; i < n; i++) if (a[i - 1] > a[i]) ok = false;
        System.out.println(sum + " " + ok);
    }
}
