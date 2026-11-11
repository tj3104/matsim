"""構造とジョブのまとめて削除の通しテスト (API と スマホ幅の画面)。テスト用のサーバーを立てて確かめる。

    CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_bulk_delete.py
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
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_bulk"))
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8028
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    server = subprocess.Popen([SERVER_PYTHON, "-m", "uvicorn", "matsimapp.main:app", "--port", str(PORT)], cwd=ROOT,
                              env={**os.environ, "MATSIM_DATA_DIR": str(DATA)}, stdout=subprocess.DEVNULL,
                              stderr=subprocess.STDOUT)
    errors = []
    try:
        for _ in range(60):
            try:
                requests.get(f"{BASE}/api/status", timeout=1)
                break
            except requests.ConnectionError:
                time.sleep(0.5)
        s = requests.Session()
        s.headers.update(H)
        s.post(f"{BASE}/api/auth/setup", json={"username": "tester", "password": "test-password-123"})
        ids = [s.post(f"{BASE}/api/structures/sample", json={"key": k}).json()["id"] for k in ("Cu", "MgO", "Cu", "MgO")]
        r = s.post(f"{BASE}/api/structures/delete", json={"ids": ids[:2] + [9999]}).json()
        check("構造をまとめて削除", r["deleted"] == 2 and r["missing"] == [9999], r)
        check("残りの構造", [x["id"] for x in s.get(f"{BASE}/api/structures").json()] == ids[:1:-1])
        potential = s.get(f"{BASE}/api/potentials").json()[0]["id"]
        md = {"name": "", "structure_id": ids[2], "potential_id": potential, "repeat": [3, 3, 3], "time_step": 1.0,
              "minimize": False, "thermo_interval": 100, "traj_interval": 0,
              "stages": [{"ensemble": "nvt", "steps": 2000, "temperature": 300}]}
        jobs = [s.post(f"{BASE}/api/md/jobs", json=md).json()["id"] for _ in range(3)]
        start = time.time()
        while time.time() - start < 120 and s.get(f"{BASE}/api/jobs/{jobs[0]}").json()["status"] != "done":
            time.sleep(1)
        r = s.post(f"{BASE}/api/jobs/delete", json={"ids": jobs}).json()
        check("実行中・待ちのジョブは残す", r["deleted"] >= 1 and r["deleted"] + len(r["skipped"]) == 3, r)
        check("空の指定はエラー", s.post(f"{BASE}/api/jobs/delete", json={"ids": []}).status_code == 422)
        for i in jobs:   # 残りが終わるのを待つ
            while s.get(f"{BASE}/api/jobs/{i}").status_code == 200 and \
                    s.get(f"{BASE}/api/jobs/{i}").json()["status"] in ("queued", "running"):
                time.sleep(1)

        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
            page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: m.type == "error" and "401" not in m.text and errors.append(m.text))
            page.on("dialog", lambda d: d.accept())
            page.goto(BASE)
            page.get_by_label("ユーザー名").fill("tester")
            page.get_by_label("パスワード").fill("test-password-123")
            page.get_by_role("button", name="ログイン").click()
            expect(page.get_by_role("heading", name="計算環境")).to_be_visible(timeout=15000)
            page.goto(f"{BASE}/#/structures")
            page.get_by_role("button", name="選択").click()
            page.get_by_role("button", name="すべて選択").click()
            page.screenshot(path=SHOTS / "d01_structures_select.png")
            page.get_by_role("button", name="選択した 2 件を削除").click()
            expect(page.get_by_text("まだ構造がありません")).to_be_visible(timeout=10000)
            check("画面: 構造をまとめて削除", True)
            page.goto(f"{BASE}/#/jobs")
            page.get_by_role("button", name="選択").click()
            boxes = page.locator("input.select-box:not([disabled])")
            count = boxes.count()
            before = len(s.get(f"{BASE}/api/jobs").json())
            boxes.first.check()
            page.screenshot(path=SHOTS / "d02_jobs_select.png")
            page.get_by_role("button", name="選択した 1 件を削除").click()
            page.wait_for_timeout(1500)
            after = len(s.get(f"{BASE}/api/jobs").json())
            check("画面: ジョブを 1 件削除", count >= 1 and after == before - 1, f"選べた {count} 件、{before} → {after} 件")
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
        shutil.rmtree(DATA, ignore_errors=True)
    check("ブラウザのエラーなし", not errors, errors)
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} OK" + (f"  失敗: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
