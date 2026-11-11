"""原子分布の通しテスト (API とスマホ幅の画面)。test_voronoi.py を KEEP_DATA=1 で流して残したデータ
(Cu を融かした MD のジョブ) を使い、構造・ジョブ・フレーム平均・元素の選択・画面を確かめる。

    KEEP_DATA=1 MATSIM_TEST_DATA=<dir> CHROMIUM_PATH=<chrome> <python> 01_test/test_voronoi.py
    MATSIM_TEST_DATA=<dir> CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_distribution.py
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_voronoi"))
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8032   # 8030 は同じ PC の simphad が使っている
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def main():
    if not (DATA / "app.db").is_file():
        sys.exit("データがありません。先に test_voronoi.py を KEEP_DATA=1 で流してください")
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
        login = s.post(f"{BASE}/api/auth/login", json={"username": "tester", "password": "test-password-123"})
        if not login.ok:
            sys.exit(f"ログインできません: {login.status_code} {login.text[:200]}")
        structures = {x["name"]: x["id"] for x in s.get(f"{BASE}/api/structures").json()}
        cu, mgo = structures["Cu (fcc)"], structures["MgO (岩塩型)"]
        job = next(j["id"] for j in s.get(f"{BASE}/api/jobs").json() if j["status"] == "done" and j["kind"] == "md")
        alloy = s.post(f"{BASE}/api/modeling/save", json={"op": "random_alloy", "structure_id": cu,
                       "options": {"composition": "Cu:1,Ni:1"}}).json()["id"]
        alloy = s.post(f"{BASE}/api/modeling/save", json={"op": "supercell", "structure_id": alloy,
                       "options": {"nx": 3, "ny": 3, "nz": 6}}).json()["id"]

        r = s.post(f"{BASE}/api/analysis/distribution", json={"structure_id": cu, "axis": "z", "bin_width": 0.5}).json()
        check("fcc 単位胞: z を 0.5 Å で数える", sum(r["total"]) == 4 and len(r["centers"]) == 8,
              f"{r['total']} 範囲 {r['range']}")
        r = s.post(f"{BASE}/api/analysis/distribution", json={"structure_id": alloy, "species": ["Ni"], "axis": "z",
                                                               "bin_width": 1.8075}).json()
        check("元素を選ぶ (Ni だけ)", r["species"] == ["Ni"] and r["n_atoms"]["Ni"] == 108, r["n_atoms"])
        check("層ごとの原子数 (0.5 格子ずつ、境目の原子もばらけない)", r["counts"]["Ni"] == [9.0] * 12,
              r["counts"]["Ni"])
        r = s.post(f"{BASE}/api/analysis/distribution", json={"structure_id": alloy, "axis": "x", "bin_width": 1}).json()
        check("選ばなければすべての元素", sorted(r["species"]) == ["Cu", "Ni"] and sum(r["total"]) == 216)
        r = s.post(f"{BASE}/api/analysis/distribution", json={"job_id": job, "axis": "y", "bin_width": 2, "frames": 5}).json()
        check("ジョブ: 最後の 5 フレームで平均", r["n_frames"] == 5 and abs(sum(r["total"]) - 864) < 1e-6, r["n_frames"])
        r = s.post(f"{BASE}/api/analysis/distribution", json={"job_id": job, "axis": "z", "bin_width": 0})
        check("分割幅 0 はエラー", r.status_code == 422)
        r = s.post(f"{BASE}/api/analysis/distribution", json={"structure_id": cu, "bin_width": 0.0001})
        check("分割が多すぎるとエラー", r.status_code == 400, r.json().get("detail"))
        r = s.post(f"{BASE}/api/analysis/distribution", json={"structure_id": mgo, "species": ["Zr"]}).json()
        check("ない元素は残りの元素で数え、知らせる", r["missing"] == ["Zr"], r["species"])

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
            page.goto(f"{BASE}/#/structures/{alloy}")
            page.get_by_role("link", name="原子分布").click()
            expect(page.get_by_role("heading", name="分布", exact=True)).to_be_visible(timeout=15000)
            page.get_by_role("button", name="Ni").click()
            page.wait_for_timeout(1500)
            page.screenshot(path=SHOTS / "p01_distribution.png", full_page=True)
            check("画面: Ni だけの数が出る", "Ni 108" in page.locator("dl.facts").inner_text())
            page.goto(f"{BASE}/#/jobs/{job}")
            page.get_by_role("link", name="原子分布").click()
            expect(page.get_by_role("heading", name="分布", exact=True)).to_be_visible(timeout=15000)
            page.wait_for_timeout(1000)
            page.screenshot(path=SHOTS / "p02_distribution_job.png", full_page=True)
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
    check("ブラウザのエラーなし", not errors, errors)
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} OK" + (f"  失敗: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
