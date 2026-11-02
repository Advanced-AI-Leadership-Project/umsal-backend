"""Local development server. Lambda uses umsal_backend.lambda_handler.handler instead."""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    import uvicorn

    uvicorn.run("umsal_backend.app:app", host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
