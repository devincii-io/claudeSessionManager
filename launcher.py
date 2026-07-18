"""PyInstaller entry point — imports csm as a package so relative imports work."""

import sys

from csm.app import main

if __name__ == "__main__":
    sys.exit(main())
