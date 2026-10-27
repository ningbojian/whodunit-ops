"""支持 `python -m app`，等价于 `python -m app.cli`。"""

from app.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
