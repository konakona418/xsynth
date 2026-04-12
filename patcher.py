
def patch_gowin_platform():
    """
    Seems that the nextpnr-himbaechel seems to have changed the command line arguments it accepts.
    
    For some reason, the original --family and --cst options are no longer accepted, 
    and instead the --vopt option must be used to pass these values.
    """
    from amaranth.vendor._gowin import GowinPlatform
    new_apicula_commands = [
        r"""
        {{invoke_tool("yosys")}}
            {{quiet("-q")}}
            {{get_override("yosys_opts")|options}}
            -l {{name}}.rpt
            {{name}}.ys
        """,
        r"""
        {{invoke_tool("nextpnr-gowin")}}
            {{quiet("--quiet")}}
            {{get_override("nextpnr_opts")|options}}
            --log {{name}}.tim
            --device {{platform.part}}
            --json {{name}}.syn.json
            --write {{name}}.pnr.json
            --vopt family={{platform._chipdb_device}}
            --vopt cst={{name}}.cst
        """,
        r"""
        {{invoke_tool("gowin_pack")}}
            -d {{platform._chipdb_device}}
            -o {{name}}.fs
            {{get_override("gowin_pack_opts")|options}}
            {{name}}.pnr.json
        """
    ]

    GowinPlatform._apicula_command_templates = new_apicula_commands

def patch_tang_nano_9k_platform(program_to_flash: bool = True):
    """
    Seems that openFPGALoader won't program to flash by default,
    so patch it as well.
    
    Also the implementation is too naive and it assumes we have openFPGALoader can be found,
    and will crash and print random sh*t if it can't be found,
    so use env vars to specify the path to the openFPGALoader binary,
    and give a helpful error message if the binary is not found.
    
    :param program_to_flash: Program to flash or RAM. If program to RAM, the program is non-persistent.
    :type program_to_flash: bool
    """
    import subprocess, os, sys, shutil
    from amaranth_boards.tang_nano_9k import TangNano9kPlatform
    
    loader_path = "openFPGALoader"
    if "OPENFPGALOADER_PATH" in os.environ:
        loader_path = os.environ["OPENFPGALOADER_PATH"]
        
    if not shutil.which(loader_path):
        raise RuntimeError(f"openFPGALoader binary not found at {loader_path}!")
    
    dll_path = ""
    if sys.platform.startswith("win"):
        os.environ["PYTHONNOUSERSITE"] = "1"
        dll_path = os.environ.get("OPENFPGALOADER_DYNLIB_PATH", "")
        if dll_path:
            os.environ["PATH"] = dll_path + os.pathsep + os.environ["PATH"]

    def patched_toolchain_program(self, products, name):
        with products.extract("{}.fs".format(name)) as bitstream_filename:
            if program_to_flash:
                cmd = [loader_path, "-b", "tangnano9k", "-f", bitstream_filename]
            else:
                cmd = [loader_path, "-b", "tangnano9k", bitstream_filename]
            try:
                subprocess.check_call(cmd, env=os.environ)
            except subprocess.CalledProcessError as e:
                if e.returncode == 3221225781 and sys.platform.startswith("win"):
                    raise RuntimeError(f"Failed to load openFPGALoader DLL! Make sure the DLL is in the PATH "\
                        "or specify its location using the OPENFPGALOADER_DYNLIB_PATH environment variable.") from e
                else:
                    raise e

    TangNano9kPlatform.toolchain_program = patched_toolchain_program
    
def patch_all(program_to_flash: bool = True):
    patch_gowin_platform()
    patch_tang_nano_9k_platform(program_to_flash)