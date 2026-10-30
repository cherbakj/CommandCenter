"""python -m commandcenter: the commands are in cli.py. This file stays this small and imports only starterror first,
so a mistake anywhere else in the program, cli.py included, ends in the error screen and not a silent exit."""
import sys

from . import starterror

try:
    from .cli import main
    main()
except Exception:
    if not starterror.shows_screen():
        raise
    starterror.report()
    sys.exit(1)
