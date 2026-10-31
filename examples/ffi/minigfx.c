#include <stdio.h>
#include <stdlib.h>
#include "minigfx.h"
struct MgWindow { char title[32]; int w, h, drawn, tick; void (*cb)(MgWindow *, void *); void *user; };
MgWindow *mg_open(const char *title, int w, int h) {
    MgWindow *win = calloc(1, sizeof *win);
    snprintf(win->title, sizeof win->title, "%s", title);
    win->w = w; win->h = h;
    printf("[minigfx] open '%s' %dx%d\n", win->title, w, h);
    return win;
}
void mg_close(MgWindow *w) { printf("[minigfx] close '%s'\n", w->title); free(w); }
void mg_fill_rect(MgWindow *w, int x, int y, int rw, int rh, MgColor c) {
    w->drawn += rw * rh;
    printf("[minigfx] rect %d,%d %dx%d rgb(%d,%d,%d)\n", x, y, rw, rh, c.r, c.g, c.b);
}
int mg_poll(MgWindow *w, MgEvent *out) {
    w->tick++;
    out->key = 0;
    out->kind = w->tick < 3 ? MG_EVENT_KEY : w->tick == 3 ? MG_EVENT_KEY : MG_EVENT_QUIT;
    if (w->tick == 3) out->key = MG_KEY_ESCAPE; else if (w->tick < 3) out->key = 'a' + w->tick;
    return 1;
}
void mg_on_draw(MgWindow *w, void (*cb)(MgWindow *, void *), void *user) { w->cb = cb; w->user = user; }
void mg_frame(MgWindow *w) { if (w->cb) w->cb(w, w->user); }
int mg_pixels_drawn(const MgWindow *w) { return w->drawn; }
