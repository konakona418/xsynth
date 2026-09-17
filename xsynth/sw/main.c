/* Phase 3b firmware: the soft core owns the command path.
 *
 * The hardware still does the wire: UART, framing and CRC. What reaches this
 * program is a stream of already-validated frames. Each one is announced by a
 * header word -- bit 15 set, the packet type in the low byte and the number of
 * body bytes in bits 13:8 -- followed by that many body bytes, one per word.
 * For PKT_COMMANDS the body is a whole number of 8-byte little-endian command
 * words.
 *
 * This program does the minimum that makes the CPU the real producer: it
 * unpacks each command and hands it to the engine through the co-processor.
 * Because the co-processor stalls while the command FIFO is full, a frame can
 * never be silently dropped here, so there is no overflow handling to get
 * wrong -- the CPU simply slows down to the engine's rate.
 *
 * Voice allocation and sequencing (deciding *which* voice a note goes to, and
 * *when*) belong here too, and land in the next step; for now every command is
 * forwarded verbatim.
 */

#include "registers.h"

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

int main(void) {
    unsigned length = 0;  /* body bytes announced by the current header */
    unsigned index = 0;   /* body bytes consumed so far */
    unsigned slot = 0;    /* byte within the current command */
    unsigned lo = 0;
    unsigned hi = 0;
    unsigned pushed = 0;  /* commands forwarded, for the host to sanity-check */

    /* Announce the image: the host reads this to tell a running program from
       the right running program. */
    REG_STATUS = FIRMWARE_MAGIC;

    for (;;) {
        unsigned status = REG_RX_STATUS;

        if (status & RX_OVERFLOW) {
            /* The stream lost a frame, so whatever is in flight is garbage.
               Flush and wait for a clean header. */
            REG_RX_DATA = 0;
            length = 0;
            index = 0;
            slot = 0;
            continue;
        }

        if (status & RX_EMPTY) {
            continue;
        }

        unsigned word = REG_RX_DATA;

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
            cmd_push(lo, hi);
            slot = 0;
            lo = 0;
            hi = 0;
            REG_COMMANDS = ++pushed;
        }
    }
}
