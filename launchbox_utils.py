from __future__ import annotations

import sys
import traceback

from launchbox_tools.cli import main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        if getattr(sys, "frozen", False):
            try:
                from tkinter import messagebox

                messagebox.showerror("LaunchBox Utils", traceback.format_exc())
            except Exception:
                print(traceback.format_exc(), file=sys.stderr)
            raise SystemExit(1) from None
        raise
