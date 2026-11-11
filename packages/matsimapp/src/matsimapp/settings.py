"""画面側の設定。計算とデータ置き場の設定は matsimapi.settings にある。"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = PACKAGE_DIR / "frontend"
DOCS_DIR = PACKAGE_DIR / "docs"

SESSION_COOKIE = "matsim_session"
SESSION_DAYS = 30
# HTTPS の中継 (Tailscale など) の後ろで動かすときに 1 にする。Cookie を HTTPS 専用にする。
COOKIE_SECURE = os.environ.get("MATSIM_COOKIE_SECURE", "0") == "1"
