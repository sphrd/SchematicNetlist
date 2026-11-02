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


def _step(text: str) -> None:
    """A new line in the bundled exe's splash log; no-op in a source run or once the splash closed."""
    try:
        import pyi_splash  # pyright: ignore[reportMissingModuleSource]

        if pyi_splash.is_alive():
            pyi_splash.update_text(text)
    except Exception:
        pass


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
            if sys.argv[2:3] == ["update"]:  # SchematicNetlist.exe --cli update [--restart]
                from schematic_netlist import appshell
                from schematic_netlist.gui import APP_INFO

                sys.exit(appshell.cli_update(APP_INFO, restart="--restart" in sys.argv))
            sys.exit(cli_main(sys.argv[2:]))
        except SystemExit:
            raise
        except Exception:
            # no console to print to: leave the traceback next to the cwd and fail quietly
            with open("SchematicNetlist_error.txt", "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            sys.exit(1)
    try:
        # every step is a line in the splash's loading log (what is being loaded right now)
        _step("Starting Python")
        import importlib

        for text, modules in (
            ("Loading the PDF libraries", "pdfplumber pypdfium2 PIL.Image"),
            ("Loading the schematic engine", "schematic_netlist.netlist schematic_netlist.circuit schematic_netlist.analyze"),
            ("Loading the Excel writer", "openpyxl"),
            ("Loading the window toolkit", "tkinter tkinterdnd2"),
            ("Loading the user interface", "schematic_netlist.appshell schematic_netlist.gui"),
        ):
            _step(text)
            for m in modules.split():
                try:
                    importlib.import_module(m)
                except ImportError:
                    pass
        if "--splash" in sys.argv[1:]:  # preview: the splash only, until clicked or Esc
            from schematic_netlist import appshell

            sys.exit(appshell.splash_preview(["pdfplumber", "pypdfium2", "PIL", "openpyxl", "tkinter", "schematic_netlist"]))
        from schematic_netlist.gui import main

        main()
    except Exception:
        _fatal(traceback.format_exc())
        raise
