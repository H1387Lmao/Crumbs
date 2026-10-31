/* A tiny fake graphics library standing in for SDL / raylib. */
#ifndef MINIGFX_H
#define MINIGFX_H
#include <stdint.h>
#define MG_EVENT_NONE 0
#define MG_EVENT_KEY 1
#define MG_EVENT_QUIT 2
#define MG_KEY_ESCAPE 27
typedef struct { uint8_t r, g, b, a; } MgColor;
typedef struct { int32_t kind; int32_t key; } MgEvent;
typedef struct MgWindow MgWindow;
MgWindow *mg_open(const char *title, int w, int h);
void mg_close(MgWindow *w);
void mg_fill_rect(MgWindow *w, int x, int y, int w_, int h_, MgColor c);
int mg_poll(MgWindow *w, MgEvent *out);
void mg_on_draw(MgWindow *w, void (*cb)(MgWindow *, void *), void *user);
void mg_frame(MgWindow *w);
int mg_pixels_drawn(const MgWindow *w);
#endif
