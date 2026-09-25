import os
import sys

if __name__ == "__main__":
    if sys.version_info < (3, 11):  # noqa: UP036 - runners may have older Python; fail clearly
        version = ".".join(map(str, sys.version_info[:3]))
        print(f"::error::Lisa needs Python 3.11 or newer on the runner; found {version}.")
        sys.exit(1)

    from lisa.review import run

    sys.exit(run(os.environ))
