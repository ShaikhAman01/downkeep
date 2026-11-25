import sys

from downkeep.cli import open_app
from downkeep.window import tell


def main() -> int:
    """Started by double-click: there is no terminal, so problems are shown as a message."""
    try:
        return open_app(None, tell=tell)
    except Exception as error:
        tell(f"Downkeep couldn't start ({type(error).__name__}). Nothing was changed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
