"""Knowledge maintenance in the engine interpreter, without an LLM call."""
from __future__ import annotations

import json
import sys


def handle(request: dict) -> dict:
    """The knowledge page's list, read and save, in this process: it is a knowledge process.

    The operations are the knowledge package's own (`finesub_knowledge.manage`);
    this module stays the door `LocalProvider.knowledge` starts.
    """

    from finesub_knowledge.manage import handle as manage

    return manage(request)


def main() -> None:
    from .download_route import install_app_proxy_policy

    install_app_proxy_policy()
    try:
        result = {"result": handle(json.load(sys.stdin))}
    except Exception as exc:
        result = {"error": str(exc)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
