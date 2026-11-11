"""モデリング画面の通しテスト (スマホ幅)。テスト用のサーバーを立てて、
構造の一覧 → モデリングのメニュー → 置換のプレビュー → 保存 → 構造画面 → グラニュラー構造、
と計算の設定画面 (入力欄の部品を共通化したので崩れていないか) をブラウザで操作し、スクリーンショットを残す。

    CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_ui_modeling.py
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_ui_modeling"))
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8023
BASE = f"http://127.0.0.1:{PORT}"
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    SHOTS.mkdir(exist_ok=True)
    log = open(DATA.parent / "test_ui_modeling_server.log", "w")
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
            # ログイン前の /api/me が 401 になるのは正常なので数えない
            page.on("console", lambda m: m.type == "error" and "401" not in m.text and errors.append(m.text))
            page.goto(BASE)
            page.get_by_label("ユーザー名").fill("tester")
            page.get_by_label("パスワード").fill("test-password-123")
            page.get_by_role("button", name="アカウントを作成").click()
            expect(page.get_by_role("heading", name="計算環境")).to_be_visible(timeout=15000)

            page.goto(f"{BASE}/#/structures")
            page.get_by_role("button", name="Cu (fcc)").click()
            expect(page.get_by_role("heading", name="この構造を加工する")).to_be_visible(timeout=15000)
            page.screenshot(path=SHOTS / "m01_structure_model_menu.png", full_page=True)

            page.locator("summary", has_text="セル・変形").click()
            page.get_by_role("link", name="スーパーセル").first.click()
            expect(page.get_by_role("heading", name="スーパーセル")).to_be_visible()
            page.get_by_role("button", name="プレビュー").click()
            expect(page.get_by_text("Cu32")).to_be_visible(timeout=30000)
            page.get_by_role("button", name="構造として保存する").click()
            expect(page.get_by_role("heading", name="この構造を加工する")).to_be_visible(timeout=15000)

            page.get_by_role("link", name="置換").first.click()
            expect(page.get_by_role("heading", name="置換")).to_be_visible()
            page.locator("label.field", has_text="割合").locator("input").fill("25")
            page.locator("label.field", has_text="割合").locator("input").dispatch_event("change")
            page.get_by_role("button", name="プレビュー").click()
            expect(page.get_by_text("Cu24Ni8")).to_be_visible(timeout=30000)
            page.screenshot(path=SHOTS / "m02_substitute_preview.png", full_page=True)
            page.get_by_role("button", name="構造として保存する").click()
            expect(page.get_by_text("モデリング (置換")).to_be_visible(timeout=15000)
            page.screenshot(path=SHOTS / "m03_saved_structure.png", full_page=True)

            page.goto(f"{BASE}/#/structures")
            expect(page.get_by_role("heading", name="モデリング (作る・加工する)")).to_be_visible(timeout=15000)
            page.locator("summary", has_text="多結晶・組織").click()
            page.screenshot(path=SHOTS / "m04_structures_menu.png", full_page=True)
            page.get_by_role("link", name="グラニュラー構造 (円柱粒)").click()
            expect(page.get_by_role("heading", name="グラニュラー構造 (円柱粒)")).to_be_visible()
            page.locator("select").first.select_option(label="Cu (fcc) (Cu4・4 原子)")
            page.get_by_role("button", name="プレビュー").click()
            for _ in range(120):
                if page.locator(".problem").count() or page.get_by_role("heading", name="結果").count():
                    break
                page.wait_for_timeout(500)
            if page.locator(".problem").count():
                errors.append("グラニュラー: " + page.locator(".problem").inner_text())
                page.screenshot(path=SHOTS / "m05_granular_error.png", full_page=True)
            expect(page.get_by_role("heading", name="結果")).to_be_visible(timeout=60000)
            page.wait_for_timeout(500)
            page.screenshot(path=SHOTS / "m05_granular_preview.png", full_page=True)

            page.goto(f"{BASE}/#/model/create")
            expect(page.get_by_role("heading", name="結晶を作る")).to_be_visible()
            page.locator("label.field", has_text="格子").locator("select").select_option("hcp")
            expect(page.locator("label.field", has_text="格子定数 c")).to_be_visible()
            page.screenshot(path=SHOTS / "m06_create_form.png", full_page=True)

            page.goto(f"{BASE}/#/home")
            expect(page.get_by_text("Atomsk (モデリング)")).to_be_visible(timeout=15000)
            tile = page.locator("a.tile[href^='#/calc/']").first
            tile.click()
            expect(page.get_by_role("heading", name="計算の条件")).to_be_visible(timeout=15000)
            fields = page.locator(".form-fields label").count()
            print("計算の設定画面の入力欄:", fields)
            if fields == 0:
                errors.append("計算の設定画面に入力欄がありません")
            page.screenshot(path=SHOTS / "m07_calc_form.png", full_page=True)
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
        shutil.rmtree(DATA, ignore_errors=True)
    print("ブラウザのエラー:", errors or "なし")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
