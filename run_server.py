"""本地演示用 uvicorn 启动器（向 sys.path 注入 .pylibs，兼容 python -I）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYLIBS = ROOT / ".pylibs" / "lib" / "python3.11" / "site-packages"
if PYLIBS.exists() and str(PYLIBS) not in sys.path:
    sys.path.insert(0, str(PYLIBS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=int(sys.argv[1]) if len(sys.argv) > 1 else 18000,
    )
