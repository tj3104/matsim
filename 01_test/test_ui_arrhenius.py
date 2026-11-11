"""アレニウス解析の画面テスト (スマホ幅)。test_arrhenius_api.py を KEEP_DATA=1 で流して残したデータを使い、
温度スキャンの入力欄 → アレニウス解析の画面 → 点を外す → ジョブ詳細からのリンク を操作してスクリーンショットを残す。

    KEEP_DATA=1 MATSIM_TEST_DATA=<dir> ~/.venv/gpumd312/bin/python 01_test/test_arrhenius_api.py
    MATSIM_TEST_DATA=<dir> CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_ui_arrhenius.py
"""
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_arrhenius"))
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8025
BASE = f"http://127.0.0.1:{PORT}"
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))


def main():
    db = sqlite3.connect(DATA / "app.db")
    series = db.execute("SELECT series FROM jobs WHERE series IS NOT NULL LIMIT 1").fetchone()
    if series is None:
        sys.exit("温度スキャンのジョブがありません。先に test_arrhenius_api.py を KEEP_DATA=1 で流してください")
    series = series[0]
    first_job = db.execute("SELECT id FROM jobs WHERE series = ? ORDER BY id", (series,)).fetchone()[0]
    db.close()
    log = open(DATA.parent / "test_ui_arrhenius_server.log", "w")
    server = subprocess.Popen([SERVER_PYTHON, "-m", "uvicorn", "matsimapp.main:app", "--port", str(PORT)],
                              cwd=ROOT, env={**os.environ, "MATSIM_DATA_DIR": str(DATA)}, stdout=log,
                              stderr=subprocess.STDOUT)
    errors = []
    try:
        for _ in range(60):
            try:
                requests.get(f"{BASE}/api/status", timeout=1)
                break
            except requests.ConnectionError:
                time.sleep(0.5)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
            page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: m.type == "error" and "401" not in m.text and errors.append(m.text))
            page.goto(BASE)
            page.get_by_label("ユーザー名").fill("tester")
            page.get_by_label("パスワード").fill("test-password-123")
            page.get_by_role("button", name="ログイン").click()
            expect(page.get_by_role("heading", name="計算環境")).to_be_visible(timeout=15000)

            page.goto(f"{BASE}/#/calc/diffusion")
            expect(page.get_by_text("温度スキャン (アレニウスプロット用)")).to_be_visible(timeout=15000)
            page.screenshot(path=SHOTS / "a01_diffusion_scan_field.png", full_page=True)

            page.goto(f"{BASE}/#/arrhenius?series={series}")
            expect(page.get_by_role("heading", name="拡散係数 D (Cu)")).to_be_visible(timeout=20000)
            expect(page.get_by_text("活性化エネルギー Ea").first).to_be_visible()
            page.wait_for_timeout(1500)
            page.screenshot(path=SHOTS / "a02_arrhenius.png", full_page=True)
            before = page.locator("dd.value").first.inner_text()
            card = page.locator("section.card", has=page.get_by_role("heading", name="拡散係数 D (Cu)"))
            card.locator("tbody input[type=checkbox]").last.uncheck()
            page.wait_for_timeout(1500)
            after = page.locator("dd.value").first.inner_text()
            print("Ea:", before, "→", after)
            if before == after:
                errors.append("点を外しても結果が変わりません")
            page.screenshot(path=SHOTS / "a03_arrhenius_excluded.png", full_page=True)

            page.goto(f"{BASE}/#/jobs/{first_job}")
            page.get_by_role("link", name="アレニウスプロット").click()
            expect(page.get_by_role("heading", name="アレニウス解析")).to_be_visible(timeout=15000)
            page.goto(f"{BASE}/#/jobs")
            page.get_by_role("link", name="アレニウス解析 (拡散のジョブを選ぶ)").click()
            expect(page.get_by_text("ほかの拡散・イオン伝導のジョブ")).to_be_visible(timeout=15000)
            page.screenshot(path=SHOTS / "a04_arrhenius_pick.png", full_page=True)
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
    print("ブラウザのエラー:", errors or "なし")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
