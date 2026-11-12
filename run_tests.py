"""本地测试入口：向 sys.path 注入 .pylibs 后调用 pytest。

环境中解释器默认不含 .pylibs 站点目录，且需要支持 `python -I` 隔离运行，
因此不依赖 PYTHONPATH，统一在脚本内注入。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYLIBS = ROOT / ".pylibs" / "lib" / "python3.11" / "site-packages"
if PYLIBS.exists() and str(PYLIBS) not in sys.path:
    sys.path.insert(0, str(PYLIBS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(pytest.main(sys.argv[1:]))
