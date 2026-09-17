/* The allocator and the schedule. See control.h for what and why. */

#include "control.h"

/* The command word, as xsynth.protocol lays it out:
       bits 63..56  opcode
       bits 55..48  voice
       bits 47..16  value
       bits 15..0   delay
   `lo` is bits 31..0 and `hi` is bits 63..32. */

static unsigned opcode_of(unsigned hi) {
    return (hi >> 24) & 0xFFu;
}

static unsigned voice_of(unsigned hi) {
    return (hi >> 16) & 0xFFu;
}

static unsigned value_of(unsigned lo, unsigned hi) {
    return ((hi & 0xFFFFu) << 16) | (lo >> 16);
}

static unsigned delay_of(unsigned lo) {
    return lo & 0xFFFFu;
}

static unsigned with_voice(unsigned hi, unsigned voice) {
    return (hi & 0xFF00FFFFu) | (voice << 16);
}

static unsigned with_delay(unsigned lo, unsigned delay) {
    return (lo & 0xFFFF0000u) | (delay & 0xFFFFu);
}

static int per_voice(unsigned opcode) {
    return opcode == OP_NOTE_ON || opcode == OP_NOTE_OFF
        || opcode == OP_SET_FREQ || opcode == OP_SET_WAVE
        || opcode == OP_SET_AMP;
}

/* Whether `a` happened before `b`. Both are tick counts that wrap, so the
   comparison has to be modular: the difference is small and the sign is what
   matters, never the magnitude. */
static int older(unsigned a, unsigned b) {
    return (int)(a - b) < 0;
}

void control_reset(control_state *state) {
    unsigned i;

    for (i = 0; i < VOICE_COUNT; i++) {
        state->voices[i].step = 0;
        state->voices[i].state = VOICE_IDLE;
        state->voices[i].stamp = 0;
    }
    state->head = 0;
    state->count = 0;
    state->tick = 0;
    state->anchor = 0;
    state->anchored = 0;
    state->chain = 0;
    state->chained = 0;
}

/* Start a note on a voice we have already chosen. */
static int take(control_state *state, unsigned step, unsigned voice) {
    state->voices[voice].step = step;
    state->voices[voice].state = VOICE_SOUNDING;
    state->voices[voice].stamp = state->tick;
    state->tick = state->tick + 1;
    return (int)voice;
}

/* Begin a release, if it is not already under way. A second note-off for the
   same note must not move the voice back up the stealing order. */
static int let_go(control_state *state, unsigned voice) {
    if (state->voices[voice].state == VOICE_IDLE) {
        return 0;
    }
    if (state->voices[voice].state == VOICE_SOUNDING) {
        state->voices[voice].state = VOICE_RELEASING;
        state->voices[voice].stamp = state->tick;
        state->tick = state->tick + 1;
    }
    return 1;
}

/* A free voice if there is one, else the release that has had longest to
   finish, else the note that has been sounding longest. */
static int allocate(control_state *state, unsigned step) {
    unsigned i;
    int best = -1;

    for (i = 0; i < VOICE_COUNT; i++) {
        if (state->voices[i].state == VOICE_IDLE) {
            return take(state, step, i);
        }
    }
    for (i = 0; i < VOICE_COUNT; i++) {
        if (state->voices[i].state != VOICE_RELEASING) {
            continue;
        }
        if (best < 0 || older(state->voices[i].stamp, state->voices[best].stamp)) {
            best = (int)i;
        }
    }
    if (best < 0) {
        best = 0;
        for (i = 1; i < VOICE_COUNT; i++) {
            if (older(state->voices[i].stamp, state->voices[best].stamp)) {
                best = (int)i;
            }
        }
    }
    return take(state, step, (unsigned)best);
}

/* The voice playing `step`, oldest first, or -1 if the note is not playing.
   A note keeps the step it was started with: changing the frequency of a voice
   does not change which note it is. */
/* Which voice a note-off means. The step is the note's identity, so this is
   the oldest voice *still sounding* that step -- and that qualifier is the
   whole job. A melody repeats pitches, and a repeat is given a new voice
   because the first one is still releasing; if the note-off were allowed to
   pick a voice that has already let go, `let_go` would do nothing to it and
   the voice that is actually sounding would never be released. So a releasing
   voice is only chosen when nothing is sounding, which keeps a duplicate
   note-off harmless rather than harmful. */
static int find(control_state *state, unsigned step) {
    unsigned i;
    int best = -1;

    for (i = 0; i < VOICE_COUNT; i++) {
        if (state->voices[i].state != VOICE_SOUNDING) {
            continue;
        }
        if (state->voices[i].step != step) {
            continue;
        }
        if (best < 0 || older(state->voices[i].stamp, state->voices[best].stamp)) {
            best = (int)i;
        }
    }
    if (best >= 0) {
        return best;
    }
    for (i = 0; i < VOICE_COUNT; i++) {
        if (state->voices[i].state != VOICE_RELEASING) {
            continue;
        }
        if (state->voices[i].step != step) {
            continue;
        }
        if (best < 0 || older(state->voices[i].stamp, state->voices[best].stamp)) {
            best = (int)i;
        }
    }
    return best;
}

/* Turn one command into the commands to push, updating the shadow. */
static int apply(control_state *state, unsigned lo, unsigned hi,
                 control_word *out) {
    unsigned opcode = opcode_of(hi);
    unsigned voice = voice_of(hi);
    unsigned i;
    int chosen;

    if (opcode == OP_RESET) {
        control_reset(state);
        out[0].lo = lo;
        out[0].hi = hi;
        return 1;
    }

    /* A global opcode's voice field means nothing to the engine, whatever the
       host put there. */
    if (!per_voice(opcode)) {
        out[0].lo = lo;
        out[0].hi = hi;
        return 1;
    }

    if (voice != VOICE_ANY) {
        /* The engine would ignore a voice that does not exist, but the shadow
           must not believe it happened either. */
        if (voice >= VOICE_COUNT) {
            return 0;
        }
        if (opcode == OP_NOTE_ON) {
            take(state, value_of(lo, hi), voice);
        } else if (opcode == OP_NOTE_OFF) {
            let_go(state, voice);
        }
        out[0].lo = lo;
        out[0].hi = hi;
        return 1;
    }

    /* VOICE_ANY. A note can be placed, because the value field carries its
       step. The rest cannot: "which voice" and "the new value" would both want
       the value field, so naming no voice means every voice. */
    if (opcode == OP_NOTE_ON) {
        chosen = allocate(state, value_of(lo, hi));
        out[0].lo = lo;
        out[0].hi = with_voice(hi, (unsigned)chosen);
        return 1;
    }
    if (opcode == OP_NOTE_OFF) {
        chosen = find(state, value_of(lo, hi));
        if (chosen < 0) {
            return 0;
        }
        let_go(state, (unsigned)chosen);
        out[0].lo = lo;
        out[0].hi = with_voice(hi, (unsigned)chosen);
        return 1;
    }
    for (i = 0; i < VOICE_COUNT; i++) {
        out[i].lo = i == 0 ? lo : with_delay(lo, 0);
        out[i].hi = with_voice(hi, i);
    }
    return (int)VOICE_COUNT;
}

/* Hold an event for later. Refuses rather than reorders: the ring is drained
   from the front, so an event earlier than the one before it would never be
   reached in time. */
static void append(control_state *state, unsigned time, unsigned lo,
                   unsigned hi) {
    unsigned slot;

    if (state->count == SCHEDULE_ENTRIES) {
        return;
    }
    if (state->count > 0) {
        slot = (state->head + state->count - 1) & SCHEDULE_MASK;
        if (older(time, state->events[slot].time)) {
            return;
        }
    }
    slot = (state->head + state->count) & SCHEDULE_MASK;
    state->events[slot].time = time;
    state->events[slot].lo = with_delay(lo, 0);
    state->events[slot].hi = hi;
    state->count = state->count + 1;
}

int control_command(control_state *state, unsigned lo, unsigned hi,
                    control_word *out) {
    unsigned opcode = opcode_of(hi);

    if (opcode == OP_SCHEDULE_AT) {
        state->anchor = value_of(lo, hi);
        state->anchored = 1;
        return 0;
    }
    if (opcode == OP_CLEAR_SCHEDULE) {
        /* Clearing the schedule also stops scheduling: "drop what is pending"
           and "I am done planning ahead" are the same intention, and a host
           that wants to keep planning re-anchors with one command. Without
           this there would be no way back to immediate at all, because the
           anchor is otherwise sticky for as long as the firmware runs. */
        state->head = 0;
        state->count = 0;
        state->anchored = 0;
        return 0;
    }
    /* Reset is never scheduled, however recently the host anchored. It is the
       way out, and the hardware has already acted on the same bytes -- the
       handler clears its sticky flags as they go by -- so a reset that landed
       later would leave the two ends disagreeing about when it happened. */
    if (opcode == OP_RESET) {
        return apply(state, lo, hi, out);
    }
    if (state->anchored) {
        state->anchor += delay_of(lo);
        append(state, state->anchor, lo, hi);
        return 0;
    }
    return apply(state, lo, hi, out);
}

int control_dispatch(control_state *state, unsigned now, control_word *out) {
    int n = 0;

    while (state->count > 0 && n + (int)VOICE_COUNT <= CONTROL_MAX_OUT) {
        control_event event = state->events[state->head];
        unsigned base;
        unsigned delay;
        int gap;
        int emitted;

        /* Signed, so that an event whose time has already gone by is due
           rather than half a wrap away. */
        if ((int)(event.time - now) > DISPATCH_WINDOW) {
            break;
        }

        /* While the chain is still in the engine's future, hang the next delay
           off it and the spacing stays exact. Once it has drained there is
           nothing to hang off, so re-anchor to now and take the one or two
           samples of fetch uncertainty. */
        base = (state->chained && (int)(state->chain - now) > 0)
                   ? state->chain : now;
        gap = (int)(event.time - base);
        delay = gap > 0 ? (unsigned)gap : 0u;
        if (delay > 0xFFFFu) {
            delay = 0xFFFFu;
        }

        state->head = (state->head + 1) & SCHEDULE_MASK;
        state->count = state->count - 1;

        emitted = apply(state, with_delay(event.lo, delay), event.hi, out + n);
        if (emitted > 0) {
            state->chain = base + delay;
            state->chained = 1;
        } else {
            /* Nothing landed, so nothing is chained: the next event has to
               anchor to now rather than to a time nothing occupies. */
            state->chained = 0;
        }
        n += emitted;
    }
    return n;
}
