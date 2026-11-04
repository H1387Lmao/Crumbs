#include <stdio.h>
#include <stdlib.h>
typedef struct Node { struct Node *l, *r; } Node;
static Node *make(int d) { Node *n = malloc(sizeof *n); n->l = n->r = NULL; if (d > 0) { n->l = make(d - 1); n->r = make(d - 1); } return n; }
static long long check(Node *t) { return t->l ? 1 + check(t->l) + check(t->r) : 1; }
static void drop(Node *t) { if (t->l) { drop(t->l); drop(t->r); } free(t); }
int main(void) {
    int max_depth = 16; long long total = 0;
    Node *s = make(max_depth + 1); total += check(s); drop(s);
    Node *ll = make(max_depth);
    for (int d = 4; d <= max_depth; d += 2) {
        long iters = 1L << (max_depth - d + 4);
        for (long i = 0; i < iters; i++) { Node *t = make(d); total += check(t); drop(t); }
    }
    total += check(ll); drop(ll);
    printf("%lld\n", total);
    return 0;
}
