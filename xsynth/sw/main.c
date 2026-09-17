/* Phase 3a firmware: prove the CPU boots and can write to a peripheral.
 *
 * This is deliberately tiny. Phase 3b replaces it with the real thing, which
 * owns the UART protocol, voice allocation and the command FIFO; all that
 * matters here is that a program uploaded over the wire runs and has an effect
 * the host can see.
 */

#include "registers.h"

#define MAGIC 0x12345678u

int main(void) {
    REG_STATUS = MAGIC;

    /* Spin rather than halt: the host polls REG_STATUS for the magic, and the
       free-running REG_COUNTER shows the CPU is still executing. */
    for (;;) {
    }
}
