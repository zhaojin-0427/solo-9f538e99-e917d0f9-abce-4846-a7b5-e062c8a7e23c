# -*- coding: utf-8 -*-
"""容器/本地启动入口：python -I run.py

注意：-I（隔离模式）不会把脚本所在目录加入 sys.path，因此需要显式加入，
否则 ``from app import create_app`` 在容器内会失败。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
