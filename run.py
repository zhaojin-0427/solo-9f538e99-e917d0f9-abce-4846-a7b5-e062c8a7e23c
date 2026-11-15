# -*- coding: utf-8 -*-
"""容器/本地启动入口：python -I run.py"""

import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
