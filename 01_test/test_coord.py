"""配位数 (距離で数える) と動径分布関数の通しテスト。test_transition.py を KEEP_DATA=1 で流して残したデータ
(Cu・Ti の hcp・Fe の bcc の構造と、Ti を昇温した構造相転移のジョブ) を使う。

    MATSIM_TEST_DATA=<dir> CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_coord.py
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_transition"))
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8039
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def analyze(s, body, timeout=300):
    r = s.post(f"{BASE}/api/analysis/coord", json=body)
    if not r.ok:
        return r, None
    task = r.json()["id"]
    start = time.time()
    while time.time() - start < timeout:
        data = s.get(f"{BASE}/api/jobs/{task}/coord").json()
        if data["job"]["status"] not in ("queued", "running"):
            return r, data
        time.sleep(0.5)
    raise TimeoutError("解析が終わりません")


def main():
    if not (DATA / "app.db").is_file():
        sys.exit("データがありません。先に test_transition.py を KEEP_DATA=1 で流してください")
    server = subprocess.Popen([SERVER_PYTHON, "-m", "uvicorn", "matsimapp.main:app", "--port", str(PORT)], cwd=ROOT,
                              env={**os.environ, "MATSIM_DATA_DIR": str(DATA)}, stdout=subprocess.DEVNULL,
                              stderr=subprocess.STDOUT, start_new_session=True)
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
        s.post(f"{BASE}/api/auth/login", json={"username": "tester", "password": "test-password-123"})
        structures = {x["name"]: x["id"] for x in s.get(f"{BASE}/api/structures").json()}
        if "MgO (岩塩型)" not in structures:
            structures["MgO (岩塩型)"] = s.post(f"{BASE}/api/structures/sample", json={"key": "MgO"}).json()["id"]
        cu, ti, fe, mgo = (structures[k] for k in ("Cu (fcc)", "Ti hcp", "Fe bcc", "MgO (岩塩型)"))
        # Ti を昇温した構造相転移のジョブ (hcp で始まり、途中で bcc に変わる)
        job = next(j["id"] for j in s.get(f"{BASE}/api/jobs").json()
                   if j["kind"] == "transition" and j["status"] == "done" and j["name"].startswith("Ti"))

        _, data = analyze(s, {"structure_id": cu})
        r = data["result"]
        check("fcc の Cu は 12 (自動の距離は第 1 と第 2 近接のあいだ)", r["mean"] == 12 and 2.6 < r["cutoffs"]["Cu-Cu"] < 3.6,
              (r["mean"], r["cutoffs"]))
        check("g(r) を返す", len(r["rdf"]["r"]) > 100 and max(r["rdf"]["total"]) > 1)
        _, data = analyze(s, {"structure_id": ti})
        check("hcp の Ti は 12", data["result"]["mean"] == 12, data["result"]["cutoffs"])
        _, data = analyze(s, {"structure_id": fe})
        check("bcc の Fe (完全な結晶) は 8", data["result"]["mean"] == 8, data["result"]["cutoffs"])
        _, data = analyze(s, {"structure_id": fe, "cutoff": 3.5})
        check("数える距離を 3.5 Å にすると bcc は 14 (第 2 近接まで)", data["result"]["mean"] == 14
              and data["result"]["cutoff_auto"] is False)
        _, data = analyze(s, {"structure_id": mgo})
        m = data["result"]["matrix"]
        check("MgO: Mg のまわりの O は 6、O のまわりの Mg は 6", m["Mg"]["O"] == 6 and m["O"]["Mg"] == 6, m)
        check("MgO: 組み合わせごとの g(r) と距離", {"Mg-O", "Mg-Mg", "O-O"} <= set(data["result"]["rdf"]["partial"])
              and 2.2 < data["result"]["cutoffs"]["Mg-O"] < 3.2, data["result"]["cutoffs"])
        r, _ = analyze(s, {"structure_id": cu, "cutoff": 0.1})
        check("数える距離が小さすぎるとエラー", r.status_code == 422, r.status_code)
        _, data = analyze(s, {"job_id": job, "frames": 30})
        evo = data["result"].get("evolution", {})
        check("ジョブ: 時間変化 (平均の配位数)", len(evo.get("time_ps", [])) >= 20 and "全体" in evo.get("mean", {}),
              {k: (round(v[0], 2), round(v[-1], 2)) for k, v in evo.get("mean", {}).items()})
        mean = evo.get("mean", {}).get("全体", [0])
        check("ジョブ: フレームごとに距離を決め直すと、始めの hcp は 12", evo.get("per_frame") and mean[0] == 12
              and len(evo.get("cutoff", {}).get("Ti-Ti", [])) == len(mean), [round(v, 1) for v in mean[:3]])
        _, fixed = analyze(s, {"job_id": job, "frames": 30, "per_frame": False})
        fixed_evo = fixed["result"]["evolution"]
        check("ジョブ: 決め直さないと、すべてのフレームで同じ距離", not fixed_evo["per_frame"]
              and len(set(fixed_evo["cutoff"]["Ti-Ti"])) == 1)
        again = s.post(f"{BASE}/api/analysis/coord", json={"job_id": job, "frames": 30}).json()
        check("同じ条件は使い回す", again["reused"])

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
            page.goto(f"{BASE}/#/structures/{mgo}")
            page.get_by_role("link", name="配位数", exact=True).click()
            page.get_by_role("button", name="解析を始める").click()
            expect(page.get_by_role("heading", name="動径分布関数 g(r)")).to_be_visible(timeout=60000)
            page.wait_for_timeout(2500)
            facts = page.locator("dl.facts").first.inner_text()
            check("画面: 既定は Mg のまわりの O で 6 個", "6.00 個" in facts, facts.replace("\n", " "))
            check("画面: g(r) と分布のグラフが描かれる", page.locator(".chart .main-svg").count() >= 2)
            check("画面: 組み合わせの表で選んだマスに色", page.locator("td.cell-on").inner_text() == "6.00")
            page.screenshot(path=SHOTS / "c01_coord_mgo.png", full_page=True)
            page.locator("label.field", has_text="数えるとなりの元素").locator("select").select_option("*")
            page.wait_for_timeout(800)
            check("画面: となりを「すべて」にすると 18 個", "18.00 個" in page.locator("dl.facts").first.inner_text())
            check("画面: 色を付けた原子の数が出る", "原子に色を付けています" in str(page.locator("p.note").all_inner_texts()))
            check("画面: null が出ない", "null" not in page.locator("body").inner_text())
            # 3D 表示に色 (青〜赤) が付いているか: 色の付いた画素 (RGB の差が大きい) を数える
            from PIL import Image
            page.locator("canvas.viewer").screenshot(path=SHOTS / "c03_coord_colored.png")
            pixels = list(Image.open(SHOTS / "c03_coord_colored.png").convert("RGB").getdata())
            colored = sum(1 for r, g, b in pixels if max(r, g, b) - min(r, g, b) > 80)
            check("画面: 3D 表示の原子に色が付く (黒くならない)", colored > 500, colored)
            page.goto(f"{BASE}/#/jobs/{job}")
            page.get_by_role("link", name="配位数", exact=True).click()
            page.get_by_role("button", name="解析を始める").click()
            expect(page.get_by_role("heading", name="平均の配位数の時間変化")).to_be_visible(timeout=60000)
            page.wait_for_timeout(2500)
            page.screenshot(path=SHOTS / "c02_coord_job.png", full_page=True)
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
