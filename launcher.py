"""PyInstaller entry point — imports asm as a package so relative imports work."""

import sys

from asm.app import main

if __name__ == "__main__":
    sys.exit(main())
