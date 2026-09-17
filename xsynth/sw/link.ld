/* Firmware image for the Xsynth SoC.
 *
 * The CPU is reset to address 0, so the startup code has to be the first thing
 * in the image; every other section just follows it. There is one memory: the
 * program, the data and the stack all live in the same BSRAM.
 */

OUTPUT_ARCH(riscv)
ENTRY(_start)

MEMORY {
    ram (rwx) : ORIGIN = 0x00000000, LENGTH = 8K
}

SECTIONS {
    .text : {
        *(.text.init)
        *(.text*)
    } > ram

    .rodata : ALIGN(4) {
        *(.rodata*)
        *(.srodata*)
    } > ram

    .data : ALIGN(4) {
        *(.data*)
        *(.sdata*)
    } > ram

    .bss : ALIGN(4) {
        __bss_start = .;
        *(.bss*)
        *(.sbss*)
        *(COMMON)
        __bss_end = .;
    } > ram

    _stack_top = ORIGIN(ram) + LENGTH(ram);
}
