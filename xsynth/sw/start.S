/* Reset vector and C runtime entry for the Xsynth SoC.
 *
 * This must stay first in the image: PicoRV32 is built with
 * PROGADDR_RESET = 0, so whatever lands at address 0 is the first thing it
 * executes. There is no bootloader and no interrupt table yet.
 */

    .section .text.init, "ax", @progbits
    .globl _start
    .type _start, @function
_start:
    la      sp, _stack_top

    /* Zero .bss, because a BSRAM powers up with undefined contents. */
    la      t0, __bss_start
    la      t1, __bss_end
    bgeu    t0, t1, 2f
1:
    sw      zero, 0(t0)
    addi    t0, t0, 4
    bltu    t0, t1, 1b
2:
    call    main

    /* main is not supposed to return; if it does, spin rather than run off
       into whatever follows. */
3:
    j       3b
