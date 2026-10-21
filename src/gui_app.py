"""PyInstaller entry point for SchematicNetlist.exe -- kept outside the package (a plain script
for PyInstaller's analysis); the actual window lives in schematic_netlist/gui.py.

Built with --windowed (no console), so any startup failure needs its own
error surface -- a crashed import/init would otherwise vanish silently."""
import sys
import traceback


def _fatal(message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "SchematicNetlist - startup error", 0x10)
    except Exception:
        print(message, file=sys.stderr)


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()  # page extraction runs in worker processes of this same exe
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        # headless use of the same exe: SchematicNetlist.exe --cli analyze board.pdf ...
        import os

        if sys.stdout is None or sys.stderr is None:  # windowed build has no console
            sys.stdout = sys.stderr = open(os.devnull, "w")
        try:
            import pyi_splash

            pyi_splash.close()  # no splash for headless runs
        except ImportError:
            pass
        from schematic_netlist.cli import main as cli_main

        try:
            sys.exit(cli_main(sys.argv[2:]))
        except SystemExit:
            raise
        except Exception:
            # no console to print to: leave the traceback next to the cwd and fail quietly
            with open("SchematicNetlist_error.txt", "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            sys.exit(1)
    try:
        try:
            import pyi_splash

            pyi_splash.update_text("Loading the application...")
        except ImportError:
            pass
        from schematic_netlist.gui import main

        main()
    except Exception:
        _fatal(traceback.format_exc())
        raise
