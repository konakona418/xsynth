/* The firmware: the wiring, and nothing else.
 *
 * The hardware does the wire -- UART, framing and CRC. What reaches this
 * program is a stream of already-validated frames, each announced by a header
 * word (bit 15 set, the packet type in the low byte, the number of body bytes
 * in bits 13:8) followed by that many body bytes, one per word. For
 * PKT_COMMANDS the body is a whole number of 8-byte little-endian commands.
 *
 * The decisions -- which voice a note goes to, and when it is applied -- are
 * in control.c, which touches no register and can therefore be compiled for
 * the host and tested there. This file only moves bytes and clocks between
 * that logic and the hardware: the mailbox on one side, the co-processor on
 * the other, and REG_SAMPLES to tell the schedule what time it is.
 *
 * The co-processor stalls the CPU while the command FIFO is full, so a frame
 * can never be silently dropped here and there is no overflow handling to get
 * wrong -- the CPU slows down to the engine's rate instead.
 */

#include "registers.h"
#include "control.h"

/* Mailbox word flags. */
#define RX_HEADER 0x8000u
#define RX_TYPE 0x00ffu
#define RX_BODY 0x3f00u
#define RX_BODY_SHIFT 8
#define RX_EMPTY 0x1u
#define RX_OVERFLOW 0x2u

#define COMMAND_BYTES 8u

/* xsynth.push {rs2, rs1}: the custom-0 instruction the co-processor claims.
   It has no RISC-V side effects (rd is x0), so volatile is what keeps it. */
static void cmd_push(unsigned lo, unsigned hi) {
    register unsigned a0 asm("a0") = lo;
    register unsigned a1 asm("a1") = hi;
    asm volatile(".insn r 0x0b, 0, 0, x0, a0, a1" : : "r"(a0), "r"(a1));
}

/* Push what the logic handed back and keep the host's count of it current. */
static void emit(const control_word *out, int count, unsigned *pushed) {
    int i;

    if (count <= 0) {
        return;
    }
    for (i = 0; i < count; i++) {
        cmd_push(out[i].lo, out[i].hi);
    }
    *pushed += (unsigned)count;
    REG_COMMANDS = *pushed;
}

int main(void) {
    /* The allocator's shadow and the schedule together are most of the BSRAM,
       so they belong in .bss, not in a stack frame. */
    static control_state state;

    unsigned length = 0;  /* body bytes announced by the current header */
    unsigned index = 0;   /* body bytes consumed so far */
    unsigned slot = 0;    /* byte within the current command */
    unsigned lo = 0;
    unsigned hi = 0;
    unsigned pushed = 0;  /* commands forwarded, for the host to sanity-check */
    control_word out[CONTROL_MAX_OUT];

    control_reset(&state);

    /* Announce the image: the host reads this to tell a running program from
       the right running program. */
    REG_STATUS = FIRMWARE_MAGIC;

    for (;;) {
        unsigned now = REG_SAMPLES;
        unsigned status;
        unsigned word;

        /* Dispatch first, so that a burst of UART traffic cannot starve it. A
           pass of this loop is a few dozen cycles against the 560 a 48 kHz
           sample leaves, so a due event is late by a fraction of a sample
           rather than by the length of the burst. */
        emit(out, control_dispatch(&state, now, out), &pushed);

        status = REG_RX_STATUS;

        if (status & RX_OVERFLOW) {
            /* The stream lost a frame. There is nothing to undo: the shadow
               only moves when a command is processed, and a frame that never
               arrived was never processed here or in the engine. */
            REG_RX_DATA = 0;
            length = 0;
            index = 0;
            slot = 0;
            continue;
        }

        if (status & RX_EMPTY) {
            continue;
        }

        word = REG_RX_DATA;

        if (word & RX_HEADER) {
            if ((word & RX_TYPE) != PKT_COMMANDS) {
                continue;  /* not ours; the mailbox only forwards one type */
            }
            length = (word & RX_BODY) >> RX_BODY_SHIFT;
            index = 0;
            slot = 0;
            lo = 0;
            hi = 0;
            continue;
        }

        if (index >= length) {
            continue;  /* a body byte with no frame around it */
        }

        index++;

        if (slot < 4) {
            lo |= (word & 0xffu) << (8 * slot);
        } else {
            hi |= (word & 0xffu) << (8 * (slot - 4));
        }

        slot++;
        if (slot == COMMAND_BYTES) {
            emit(out, control_command(&state, lo, hi, out), &pushed);
            slot = 0;
            lo = 0;
            hi = 0;
        }
    }
}
