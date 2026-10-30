"""Start the ShelfLife web app on your own machine.

Run from the project root:  python -m scripts.web [--port 8000]
Then open http://127.0.0.1:8000

It only listens on 127.0.0.1 (this computer) and has no login, so don't expose it.
"""

import argparse

import uvicorn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run("shelflife.web.app:app", host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
