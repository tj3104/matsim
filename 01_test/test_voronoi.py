"""ボロノイ多面体の解析の通しテスト (API とスマホ幅の画面)。テスト用のサーバーを立てて、
Cu (864 原子) を 300 K → 2500 K に加熱して融かす MD を流し、最終構造と時間変化を解析する。

    CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_voronoi.py
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
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_voronoi"))
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8029
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def analyze(s, body, timeout=300):
    """解析のジョブを投入して、終わるまで待って結果を返す。"""
    r = s.post(f"{BASE}/api/analysis/voronoi", json=body)
    if not r.ok:
        return r, None
    task = r.json()["id"]
    start = time.time()
    while time.time() - start < timeout:
        data = s.get(f"{BASE}/api/jobs/{task}/voronoi").json()
        if data["job"]["status"] not in ("queued", "running"):
            return r, data
        time.sleep(0.5)
    raise TimeoutError("解析が終わりません")


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
        cu = s.post(f"{BASE}/api/structures/sample", json={"key": "Cu"}).json()["id"]
        mgo = s.post(f"{BASE}/api/structures/sample", json={"key": "MgO"}).json()["id"]
        _, data = analyze(s, {"structure_id": cu})
        r = data["result"]
        check("構造: fcc は <0,12,0,0> だけ", r["ranking"][0]["index"] == "<0,12,0,0>" and r["ranking"][0]["fraction"] == 1,
              r["ranking"][:2])
        check("構造: まとまりと説明が付く (fcc・hcp 100%)",
              r["families"] == [{"family": "fcc・hcp", "count": 4, "fraction": 1.0}]
              and r["ranking"][0]["detail"] == "配位数 12: 四角形 12" and r["family_of"]["<0,12,0,0>"] == "fcc・hcp",
              (r["families"], r["ranking"][0].get("detail")))
        check("解析のジョブとして一覧に出る", any(j["kind"] == "voronoi" for j in s.get(f"{BASE}/api/jobs").json()))
        _, data = analyze(s, {"structure_id": mgo, "center": "Mg"})
        check("構造: 中心の元素を選べる", data["result"]["n_center"] == 4 and data["result"]["center"] == "Mg")
        _, data = analyze(s, {"structure_id": cu, "center": "Zr"})
        check("ない元素は解析のジョブが失敗し、理由が出る", data["job"]["status"] == "failed" and "Zr" in data["job"]["message"],
              data["job"]["message"])
        r = s.post(f"{BASE}/api/analysis/voronoi", json={})
        check("対象なしはエラー", r.status_code == 400)

        potential = s.get(f"{BASE}/api/potentials").json()[0]["id"]
        md = {"name": "Cu melt", "structure_id": cu, "potential_id": potential, "repeat": [6, 6, 6], "time_step": 1.0,
              "minimize": False, "thermo_interval": 100, "traj_interval": 500,
              "stages": [{"ensemble": "nvt", "steps": 2000, "temperature": 300},
                         {"ensemble": "npt", "steps": 10000, "temperature": 300, "temperature_end": 2500, "pressure": 0},
                         {"ensemble": "npt", "steps": 6000, "temperature": 2500, "pressure": 0}]}
        job = s.post(f"{BASE}/api/md/jobs", json=md).json()["id"]
        start = time.time()
        while time.time() - start < 600 and s.get(f"{BASE}/api/jobs/{job}").json()["status"] not in ("done", "failed"):
            time.sleep(2)
        check("MD 完了", s.get(f"{BASE}/api/jobs/{job}").json()["status"] == "done")
        # 解析中に GPU のジョブを入れても、別の列なので待たずに動く
        r = s.post(f"{BASE}/api/analysis/voronoi", json={"job_id": job, "frames": 20})
        task = r.json()["id"]
        md2 = s.post(f"{BASE}/api/md/jobs", json={**md, "stages": [{"ensemble": "nvt", "steps": 3000, "temperature": 300}]}).json()["id"]
        seen, gpu_ran = set(), False
        start = time.time()
        while time.time() - start < 300:
            data = s.get(f"{BASE}/api/jobs/{task}/voronoi").json()
            seen.add(data["job"]["steps_done"])
            if data["job"]["status"] == "running" and s.get(f"{BASE}/api/jobs/{md2}").json()["status"] in ("running", "done"):
                gpu_ran = True
            if data["job"]["status"] not in ("queued", "running"):
                break
            time.sleep(0.3)
        first = time.time() - start
        res = data["result"]
        check("進み具合が増えていく", len(seen) >= 3, sorted(seen))
        check("解析中も GPU のジョブが動く", gpu_ran)
        evo = res.get("evolution") or {}
        fcc = evo.get("fractions", {}).get("<0,12,0,0>")
        check("ジョブ: 解析できる", res and res["n_atoms"] == 864, f"{first:.1f}s 上位 {[x['index'] for x in res['ranking'][:3]]}")
        check("ジョブ: 時間変化 (フレーム)", len(evo.get("time_ps", [])) >= 10, evo.get("time_ps", [])[:3])
        check("融けて fcc が減る (最初のフレームの種類も追う)", fcc and fcc[0] > 0.5 and fcc[-1] < 0.1,
              f"{fcc[0]:.2f} → {fcc[-1]:.2f}" if fcc else "fcc を追っていない")
        fam = evo.get("families", {})
        check("まとまりの時間変化: fcc・hcp が減り、乱れた多面体・二十面体系が増える",
              fam and fam["fcc・hcp"][0] > 0.5 and fam["fcc・hcp"][-1] < 0.1
              and fam.get("乱れた多面体", [0])[-1] + fam.get("二十面体に近い", [0])[-1] > 0.5
              and all(abs(sum(v[i] for v in fam.values()) - 1) < 1e-9 for i in range(len(evo["time_ps"]))),
              {k: f"{v[0]:.2f}→{v[-1]:.2f}" for k, v in fam.items()})
        check("色塗り用の指数が原子ごとにある", len(res["view_labels"]) == res["view"]["shown"])
        again = s.post(f"{BASE}/api/analysis/voronoi", json={"job_id": job, "frames": 20}).json()
        check("同じ条件は使い回す", again["reused"] and again["id"] == task, again)
        r = s.post(f"{BASE}/api/analysis/voronoi", json={"job_id": job, "frames": 200, "edge_fraction": 0.2}).json()
        s.post(f"{BASE}/api/jobs/{r['id']}/cancel")
        while s.get(f"{BASE}/api/jobs/{r['id']}").json()["status"] in ("queued", "running"):
            time.sleep(0.3)
        status = s.get(f"{BASE}/api/jobs/{r['id']}").json()["status"]
        check("解析を中止できる", status in ("cancelled", "done"), status)

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
            page.goto(f"{BASE}/#/jobs/{job}")
            page.get_by_role("link", name="ボロノイ解析", exact=True).click()
            page.locator("details summary", has_text="詳細設定").click()
            page.locator("label.field", has_text="グラフに出す種類の数").locator("input").fill("8")
            page.locator("label.field", has_text="グラフに出す種類の数").locator("input").dispatch_event("change")
            page.get_by_role("button", name="解析を始める").click()
            expect(page.get_by_text("解析の進み具合")).to_be_visible(timeout=15000)
            page.screenshot(path=SHOTS / "v00_voronoi_progress.png", full_page=True)
            expect(page.get_by_role("heading", name="ボロノイ多面体の比率")).to_be_visible(timeout=120000)
            page.wait_for_timeout(2500)
            page.screenshot(path=SHOTS / "v01_voronoi_job.png", full_page=True)
            check("画面: 既定はまとまりで見る", page.get_by_role("button", name="まとまりで見る").get_attribute("aria-pressed") == "true"
                  and page.locator(".chips").get_by_text("乱れた多面体").count() > 0)
            page.get_by_role("button", name="すべて選ぶ").click()
            page.get_by_text("選んでいない原子を隠す").click()
            page.wait_for_timeout(800)
            page.locator("canvas.viewer").screenshot(path=SHOTS / "v02_voronoi_family.png")
            check("画面: 色を付けた原子の数が出る", "原子に色を付けています" in page.locator("p.note").all_inner_texts().__str__())
            page.get_by_text("ボロノイ指数の読み方").click()
            check("画面: 読み方の説明", page.get_by_text("正二十面体が少し崩れたもの").first.is_visible())
            page.screenshot(path=SHOTS / "v04_voronoi_reading.png", full_page=True)
            page.get_by_role("button", name="指数で見る").click()
            page.get_by_role("button", name="上位 5 種類を選ぶ").click()
            page.wait_for_timeout(800)
            page.locator("canvas.viewer").screenshot(path=SHOTS / "v02_voronoi_colored.png")
            check("画面: 指数のチップに面の内訳が出る", page.locator(".chips small").first.inner_text().startswith("配位数"),
                  page.locator(".chips small").first.inner_text())
            page.screenshot(path=SHOTS / "v05_voronoi_index.png", full_page=True)
            page.goto(f"{BASE}/#/structures/{cu}")
            page.get_by_role("link", name="ボロノイ解析", exact=True).click()
            page.get_by_role("button", name="解析を始める").click()
            expect(page.locator("table").get_by_text("<0,12,0,0>", exact=True)).to_be_visible(timeout=30000)
            page.screenshot(path=SHOTS / "v03_voronoi_structure.png", full_page=True)
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
        if os.environ.get("KEEP_DATA") != "1":
            shutil.rmtree(DATA, ignore_errors=True)
    check("ブラウザのエラーなし", not errors, errors)
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} OK" + (f"  失敗: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
