/* The decisions the soft core owns: which voice a note goes to, and when.
 *
 * Nothing here touches a register. Every function is handed the current 48 kHz
 * sample count and hands back the commands that should be pushed into the
 * engine's FIFO, which is what lets the whole thing be compiled for the host
 * and driven from tests instead of from a board. `main.c` is the wiring.
 *
 * Two jobs live here:
 *
 *   the allocator   A host that names no voice (VOICE_ANY) is asking for one.
 *                   The shadow state says which are busy, which are releasing
 *                   and how recently each changed, so a new note takes a free
 *                   voice, or one whose release has had the longest to finish,
 *                   or failing that the oldest note still sounding.
 *
 *   the schedule    Commands carrying an anchor (OP_SCHEDULE_AT) accumulate
 *                   their delays into absolute sample times and are held in a
 *                   ring buffer until they come due. The engine's own
 *                   scheduler already counts samples between relative delays,
 *                   so the firmware only has to convert one into the other --
 *                   and to keep the chain alive across a gap, which is what
 *                   `chain` is for.
 */

#ifndef XSYNTH_CONTROL_H
#define XSYNTH_CONTROL_H

#include "protocol.h"

/* How many scheduled events fit in the ring buffer. Each costs twelve bytes of
   BSRAM, so this is 3 KB of the eight the SoC has -- about two minutes of
   music at four notes a second, and a host streams anyway rather than filling
   it in one go. Five hundred and twelve overflows the region and three hundred
   and eighty-four would leave four hundred bytes of stack under a call chain
   that wants more than that.

   It has to be a power of two: the ring wraps with a mask, because RV32I has no
   divider, `-nostdlib` means there is no `__umodsi3` to call, and a division in
   the dispatch path would be the only thing in the firmware that could. */
#define SCHEDULE_ENTRIES 256
#define SCHEDULE_MASK (SCHEDULE_ENTRIES - 1)

#if (SCHEDULE_ENTRIES & SCHEDULE_MASK) != 0
#error SCHEDULE_ENTRIES must be a power of two
#endif

/* How far ahead of its time an event is pushed, in 48 kHz samples. Long enough
   to cover one pass of the dispatch loop with room to spare, short enough that
   a command arriving now never waits behind more than 1.3 ms of schedule. */
#define DISPATCH_WINDOW 64

/* The most commands one call can hand back: two broadcasts to every voice, or
   sixteen single ones. Anything left over is still in the schedule and comes
   back on the next call. */
#define CONTROL_MAX_OUT 16

/* One 64-bit command, split the way the co-processor wants it. */
typedef struct {
    unsigned lo;
    unsigned hi;
} control_word;

enum {
    VOICE_IDLE = 0,
    VOICE_SOUNDING = 1,
    VOICE_RELEASING = 2,
};

typedef struct {
    unsigned step;   /* the phase increment that identifies the note */
    unsigned state;  /* VOICE_IDLE, VOICE_SOUNDING or VOICE_RELEASING */
    unsigned stamp;  /* tick at the last state change, for "oldest" */
} control_voice;

typedef struct {
    unsigned time;   /* absolute 48 kHz sample */
    unsigned lo;
    unsigned hi;
} control_event;

typedef struct {
    control_voice voices[VOICE_COUNT];
    control_event events[SCHEDULE_ENTRIES];
    unsigned head;    /* oldest event */
    unsigned count;   /* events in the ring */
    unsigned tick;    /* increments on every allocation and release */
    unsigned anchor;  /* the running absolute time, while anchored */
    unsigned anchored;
    unsigned chain;   /* the sample the last pushed event lands on */
    unsigned chained; /* whether `chain` still means anything */
} control_state;

/* Forget everything: every voice idle, no events, no anchor. */
void control_reset(control_state *state);

/* Take one command from the host. Returns how many commands to push into
   `out`, which may be none: an anchor or a clear is the firmware's business,
   and a note-off naming a note that is not playing has nowhere to go. */
int control_command(control_state *state, unsigned lo, unsigned hi,
                    control_word *out);

/* Hand back whatever has come due at `now`. */
int control_dispatch(control_state *state, unsigned now, control_word *out);

#endif
