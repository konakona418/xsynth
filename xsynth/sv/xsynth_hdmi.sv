// Xsynth glue around hdl-util/hdmi.
//
// The upstream core uses unpacked-array ports (e.g. `logic [55:0] sub [3:0]`
// and `logic [15:0] audio_sample_word [1:0]`). Amaranth's `Instance` cannot
// express those, so this wrapper flattens them and pins the video/audio format
// Xsynth supports.
//
// Configuration comes from preprocessor defines rather than module parameters:
// the Yosys slang frontend instantiates top-level modules, which strips their
// parameters, and this wrapper is a top as far as the SV compilation is
// concerned. `xsynth/platform/hdmi_patch.py` supplies the defines.
//
// `VIDEO_REFRESH_RATE` is fixed at 60.0 so that the derived `VIDEO_RATE` is the
// nominal pixel clock (no 1000/1001 factor).

`ifndef XSYNTH_VIDEO_ID_CODE
`define XSYNTH_VIDEO_ID_CODE 1
`endif

`ifndef XSYNTH_BIT_WIDTH
`define XSYNTH_BIT_WIDTH 10
`endif

`ifndef XSYNTH_BIT_HEIGHT
`define XSYNTH_BIT_HEIGHT 10
`endif

`ifndef XSYNTH_DVI_OUTPUT
`define XSYNTH_DVI_OUTPUT 1
`endif

`ifndef XSYNTH_AUDIO_RATE
`define XSYNTH_AUDIO_RATE 48000
`endif

`ifndef XSYNTH_AUDIO_BITS
`define XSYNTH_AUDIO_BITS 16
`endif

module xsynth_hdmi (
    input  logic clk_pixel,
    input  logic clk_pixel_x5,
    input  logic clk_audio,
    input  logic reset,
    input  logic [23:0] rgb,
    input  logic [`XSYNTH_AUDIO_BITS-1:0] audio_left,
    input  logic [`XSYNTH_AUDIO_BITS-1:0] audio_right,

    output logic [2:0] tmds,
    output logic tmds_clock,

    output logic [`XSYNTH_BIT_WIDTH-1:0] cx,
    output logic [`XSYNTH_BIT_HEIGHT-1:0] cy,
    output logic [`XSYNTH_BIT_WIDTH-1:0] frame_width,
    output logic [`XSYNTH_BIT_HEIGHT-1:0] frame_height,
    output logic [`XSYNTH_BIT_WIDTH-1:0] screen_width,
    output logic [`XSYNTH_BIT_HEIGHT-1:0] screen_height
);
    hdmi #(
        .VIDEO_ID_CODE(`XSYNTH_VIDEO_ID_CODE),
        .IT_CONTENT(1'b1),
        .DVI_OUTPUT(`XSYNTH_DVI_OUTPUT),
        .VIDEO_REFRESH_RATE(60.0),
        .AUDIO_RATE(`XSYNTH_AUDIO_RATE),
        .AUDIO_BIT_WIDTH(`XSYNTH_AUDIO_BITS),
        .VENDOR_NAME({"Xsynth", 16'd0}),
        .PRODUCT_DESCRIPTION({"Xsynth FPGA", 40'd0}),
        .SOURCE_DEVICE_INFORMATION(8'h00)
    ) u_hdmi (
        .clk_pixel(clk_pixel),
        .clk_pixel_x5(clk_pixel_x5),
        .clk_audio(clk_audio),
        .reset(reset),
        .rgb(rgb),
        .audio_sample_word('{audio_left, audio_right}),
        .tmds(tmds),
        .tmds_clock(tmds_clock),
        .cx(cx),
        .cy(cy),
        .frame_width(frame_width),
        .frame_height(frame_height),
        .screen_width(screen_width),
        .screen_height(screen_height)
    );
endmodule
