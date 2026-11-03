public class Bintrees {
    static final class Node { Node l, r; }
    static Node make(int d) { Node n = new Node(); if (d > 0) { n.l = make(d - 1); n.r = make(d - 1); } return n; }
    static long check(Node t) { return t.l == null ? 1 : 1 + check(t.l) + check(t.r); }
    public static void main(String[] a) {
        int maxDepth = 16;
        long total = check(make(maxDepth + 1));
        Node longLived = make(maxDepth);
        for (int d = 4; d <= maxDepth; d += 2) {
            int iters = 1 << (maxDepth - d + 4);
            for (int i = 0; i < iters; i++) total += check(make(d));
        }
        total += check(longLived);
        System.out.println(total);
    }
}
