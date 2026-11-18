"""
Builds RBXLimiter.exe with Nuitka.  Run through build_nuitka.bat (uses Python 3.14).

Why this is a Python script and not just a .bat: Nuitka has to be told where Tcl/Tk
(the library tkinter draws with) lives. Newer Python versions ship Tcl/Tk 9, which
keeps its files packed *inside* tcl90.dll / tcl9tk90.dll instead of in a folder, so
Nuitka can't find them. This script asks tkinter where they really are and, if they're
packed, unpacks them into build_tcl\\ first.
"""
import os
import shutil
import subprocess
import sys

WANT = "3.14.10"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "build_tcl")
ICON = os.path.join(HERE, "rbx_limiter.ico")


def fail(msg):
    print("\nERROR: " + msg)
    sys.exit(1)


def find_tcl_tk():
    """Return (tcl_dir, tk_dir) as real folders on disk."""
    try:
        import tkinter
    except ImportError:
        fail("this Python has no tkinter. Re-run the Python installer, choose Modify and "
             "tick 'tcl/tk and IDLE'.")
    root = tkinter.Tk()          # loads Tcl *and* Tk exactly like the app does
    root.withdraw()
    try:
        tcl_lib = root.tk.eval("info library")
        tk_lib = root.tk.eval("set tk_library")
        print(f"Tcl {root.tk.eval('info patchlevel')}  library: {tcl_lib}")
        print(f"Tk  library: {tk_lib}")

        result = []
        for name, lib in (("tcl", tcl_lib), ("tk", tk_lib)):
            if os.path.isdir(lib):                       # normal folder on disk
                result.append(os.path.normpath(lib))
                continue
            # Packed inside the DLL (Tcl 9 "zipfs") -> copy it out with Tcl itself.
            dst = os.path.join(OUT, name)
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            os.makedirs(OUT, exist_ok=True)
            print(f"  {name} files are packed inside the DLL - unpacking to {dst}")
            root.tk.eval(f"file copy -force {{{lib}}} {{{dst.replace(os.sep, '/')}}}")
            if not os.path.isdir(dst):
                fail(f"could not unpack the {name} library from {lib}")
            result.append(dst)
        return tuple(result)
    finally:
        root.destroy()


def main():
    os.chdir(HERE)
    got = ".".join(map(str, sys.version_info[:3]))
    print(f"Using Python {got}  ({sys.executable})")
    if got != WANT:
        ans = input(f"WARNING: wanted Python {WANT} but this is {got}. Build anyway? [y/N] ")
        if ans.strip().lower() != "y":
            sys.exit(1)
    for f in ("rbx_limiter.py", "scroom_assets.py"):
        if not os.path.isfile(f):
            fail(f"{f} must be in the same folder as this script ({HERE}).")

    tcl_dir, tk_dir = find_tcl_tk()
    for d, marker in ((tcl_dir, "init.tcl"), (tk_dir, "tk.tcl")):
        if not os.path.isfile(os.path.join(d, marker)):
            print(f"  note: {marker} not found in {d} - Nuitka may still complain")

    print("\nInstalling / updating Nuitka...")
    subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade",
                     "nuitka", "ordered-set", "zstandard"])

    print("\nBuilding RBXLimiter.exe (first build takes several minutes)...\n")
    cmd = [sys.executable, "-m", "nuitka",
           "--onefile",
           "--assume-yes-for-downloads",       # lets Nuitka download a C compiler itself
           "--lto=no",
           "--enable-plugin=tk-inter",
           f"--tcl-library-dir={tcl_dir}",
           f"--tk-library-dir={tk_dir}",
           "--windows-console-mode=disable",
           "--windows-uac-admin",
           "--python-flag=no_docstrings",
           "--remove-output",
           "--output-filename=RBXLimiter.exe",
           *([f"--windows-icon-from-ico={ICON}"] if os.path.isfile(ICON) else []),
           "rbx_limiter.py"]
    code = subprocess.call(cmd)
    if code != 0 and not os.path.isfile("RBXLimiter.exe"):
        # No Visual Studio and MinGW isn't allowed on 3.13+: let Nuitka fetch the Zig compiler.
        print("\nRetrying with the Zig C compiler (Nuitka downloads it automatically)...\n")
        code = subprocess.call(cmd[:3] + ["--zig"] + cmd[3:])
    if code == 0 and os.path.isfile("RBXLimiter.exe"):
        print("\nDone: " + os.path.join(HERE, "RBXLimiter.exe"))
    else:
        fail("the build failed - scroll up for Nuitka's message "
             "(and nuitka-crash-report.xml if it made one).")


if __name__ == "__main__":
    main()
