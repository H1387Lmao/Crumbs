public class Matmul {
    public static void main(String[] x) {
        int n = 600;
        double[] a = new double[n * n], b = new double[n * n], c = new double[n * n];
        for (int i = 0; i < n; i++) for (int j = 0; j < n; j++) { a[i * n + j] = (i + j) % 10; b[i * n + j] = (i * 3 + j) % 10; }
        for (int i = 0; i < n; i++) for (int k = 0; k < n; k++) { double aik = a[i * n + k]; for (int j = 0; j < n; j++) c[i * n + j] += aik * b[k * n + j]; }
        double trace = 0; for (int i = 0; i < n; i++) trace += c[i * n + i];
        System.out.println((long) trace);
    }
}
