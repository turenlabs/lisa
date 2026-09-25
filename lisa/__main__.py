import os
import sys
import traceback

from lisa.main import run


def main() -> int:
    try:
        return run(os.environ)
    except Exception as error:
        traceback.print_exc()
        message = str(error).replace("\n", " ")
        print(f"::error::{message}")
        return 1


sys.exit(main())
