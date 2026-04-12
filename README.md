# My Amaranth - TangNano9k/Gowin Template

I'm not using the large Gowin IDE, instead I use tools from the YoWASP project,
which compiles some open-source implementations to WASM.

Use uv to install the dependencies, then in dot env file:

```bash
YOSYS=yowasp-yosys
NEXTPNR_GOWIN=yowasp-nextpnr-himbaechel-gowin
GOWIN_PACK=gowin_pack

OPENFPGALOADER_PATH="/path/to/loader/openFPGALoader"
```

Note that you must install openFPGALoader manually, with OSS CAD suite or separately.
Unlike other tools, YoWASP project cannot distribute openFPGALoader due to the terrible design of WASM,
as it requires to interact with USB.
For its NPM package, it uses WebUSB, which requires runtime (Node.JS or the browser) to support such features,
but a bare wasmtime runtime - which other YoWASP project uses - doesn't have such features.

Also, if the openFPGALoader cannot find its DLLs on Windows, add:

```bash
OPENFPGALOADER_DYNLIB_PATH="/path/to/loader-dynlibs"
```
