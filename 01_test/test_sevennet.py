"""SevenNet (GNN 系の汎用力場、ASE で計算) の通しテスト。テスト用のサーバーを立てて、7net-0 で
Cu の MD (NVT → NPT の昇温)・Cu の静的計算 (構造緩和・弾性定数・状態方程式)・Si のフォノン分散 (振動のようすも) を流し、
GPUMD の計算と同じように結果・グラフ・画面が出るか、使えない計算は止まるかを確かめる。

    CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_sevennet.py
"""
import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import requests
from ase.build import bulk
from ase.io import write
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_sevennet"))
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8042
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def wait(s, job, timeout=900):
    start = time.time()
    while time.time() - start < timeout:
        try:
            status = s.get(f"{BASE}/api/jobs/{job}").json()
        except requests.ConnectionError:
            time.sleep(1)
            continue
        if status["status"] not in ("queued", "running"):
            return status
        time.sleep(3)
    raise TimeoutError("ジョブが終わりません")


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    server = subprocess.Popen([SERVER_PYTHON, "-m", "uvicorn", "matsimapp.main:app", "--port", str(PORT)], cwd=ROOT,
                              env={**os.environ, "MATSIM_DATA_DIR": str(DATA)},
                              stdout=open(ROOT / "01_test" / "test_sevennet_server.log", "w"), stderr=subprocess.STDOUT,
                              start_new_session=True)
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
        pots = s.get(f"{BASE}/api/potentials").json()
        seven = {p["name"]: p for p in pots if p.get("engine") == "sevennet"}
        check("ポテンシャルの一覧: NEP89 が先、SevenNet が並ぶ (Nano 4 つを含む 8 つ)", pots[0]["engine"] == "gpumd" and len(seven) == 8
              and all(p["available"] for p in seven.values()), [p["name"] for p in pots])
        sn = seven["SevenNet-0 (7net-0、速い)"]["id"]
        check("SevenNet の対応元素は 89 (Po などは除く)", len(seven["SevenNet-0 (7net-0、速い)"]["elements"]) == 89
              and "Po" not in seven["SevenNet-0 (7net-0、速い)"]["elements"])
        nano = seven["SevenNet-Nano 5.5 (軽量、おすすめ)"]["id"]
        cu = s.post(f"{BASE}/api/structures/sample", json={"key": "Cu"}).json()["id"]
        buf = io.StringIO()
        write(buf, bulk("Si", "diamond", a=5.43), format="vasp", direct=True)
        si = s.post(f"{BASE}/api/structures/upload", files={"file": ("POSCAR", buf.getvalue())},
                    data={"name": "Si prim"}).json()["id"]

        md = {"name": "Cu 7net", "structure_id": cu, "potential_id": sn, "repeat": [3, 3, 3], "time_step": 2.0,
              "minimize": False, "thermo_interval": 50, "traj_interval": 100,
              "stages": [{"ensemble": "nvt", "steps": 400, "temperature": 300},
                         {"ensemble": "npt", "steps": 400, "temperature": 300, "temperature_end": 900, "pressure": 0}]}
        preview = s.post(f"{BASE}/api/md/preview", json=md).json()
        check("MD の確認画面: ASE で計算する注意と計算の指示", any("SevenNet" in n for n in preview["notes"])
              and "ase_job.json" in preview["run_in"] and preview["n_atoms"] == 108, preview["notes"])
        job_md = s.post(f"{BASE}/api/md/jobs", json=md).json()["id"]
        static = s.post(f"{BASE}/api/calc/jobs", json={"kind": "static", "structure_id": cu, "potential_id": sn,
                        "repeat": [1, 1, 1], "options": {"relax": True, "relax_cell": True, "elastic": True,
                                                          "cohesive": True}}).json()["id"]
        phonon = s.post(f"{BASE}/api/calc/jobs", json={"kind": "phonon", "structure_id": si, "potential_id": sn,
                        "repeat": [1, 1, 1], "options": {"path": "GXWKGL"}}).json()["id"]
        nano_static = s.post(f"{BASE}/api/calc/jobs", json={"kind": "static", "structure_id": cu, "potential_id": nano,
                             "repeat": [1, 1, 1], "options": {"relax": True, "relax_cell": True, "elastic": True,
                                                               "cohesive": False}}).json()["id"]
        r = s.post(f"{BASE}/api/calc/preview", json={"kind": "diffusion", "structure_id": cu, "potential_id": sn,
                   "repeat": [3, 3, 3], "options": {}})
        check("使えない計算 (拡散) は理由を示して止まる", r.status_code == 400 and "SevenNet" in r.json()["detail"],
              r.json().get("detail"))

        status = wait(s, job_md)
        check("MD: 完了", status["status"] == "done", status.get("message"))
        thermo = s.get(f"{BASE}/api/jobs/{job_md}/thermo").json()
        check("MD: 熱力学量のグラフのデータ (2 ステージ、16 行)", thermo["n_rows"] == 16 and len(thermo["stages"]) == 2,
              (thermo["n_rows"], len(thermo["stages"])))
        temps = [v for v in thermo["series"]["temperature"] if v is not None]
        check("MD: 昇温で温度が上がる", np.mean(temps[-3:]) > np.mean(temps[4:8]) + 200, (round(temps[5]), round(temps[-1])))
        files = {f["name"] for f in s.get(f"{BASE}/api/jobs/{job_md}").json()["files"]}
        check("MD: 最終構造・トラジェクトリ・計算の指示", {"CONTCAR", "dump.xyz", "ase_job.json", "restart.xyz"} <= files,
              sorted(files)[:12])
        check("MD: 速さを記録 (所要時間の目安に使う)", (s.get(f"{BASE}/api/jobs/{job_md}").json().get("speed") or 0) > 0)
        saved = s.post(f"{BASE}/api/jobs/{job_md}/save-final", json={}).json()
        check("MD: 最終構造を登録できる", saved.get("id"))

        status = wait(s, static)
        res = s.get(f"{BASE}/api/jobs/{static}/results").json()
        h = {x["label"]: x["value"] for x in res["highlights"]}
        check("静的計算: 完了し、体積弾性率が 100〜200 GPa", status["status"] == "done" and 100 < h.get("体積弾性率 B", 0) < 200,
              {k: h[k] for k in list(h)[:6]})
        check("静的計算: 弾性定数の表と状態方程式", {"elastic", "cohesive"} <= {d["key"] for d in res["datasets"]})

        status = wait(s, nano_static)
        res = s.get(f"{BASE}/api/jobs/{nano_static}/results").json()
        h = {x["label"]: x["value"] for x in res["highlights"]}
        ajob = s.get(f"{BASE}/api/jobs/{nano_static}/file", params={"name": "ase_job.json"}).text
        check("Nano: 7net-nano-5.5 で計算し、体積弾性率が 100〜200 GPa", status["status"] == "done"
              and "7net-nano-5.5" in ajob and 100 < h.get("体積弾性率 B", 0) < 200,
              (status.get("message"), h.get("体積弾性率 B")))
        status = wait(s, phonon)
        check("フォノン: 完了", status["status"] == "done", status.get("message"))
        info = s.get(f"{BASE}/api/jobs/{phonon}/phonon").json()
        m = s.get(f"{BASE}/api/jobs/{phonon}/phonon/mode", params={"q": 100, "band": 0}).json()
        nu = sorted(m["frequencies"])
        check("フォノン: X 点の振動数が 2 つずつ縮退", info["n_bands"] == 6 and m["near"] == "X"
              and all(abs(nu[i] - nu[i + 1]) < 0.05 for i in (0, 2, 4)), [round(v, 2) for v in nu])
        g = sorted(s.get(f"{BASE}/api/jobs/{phonon}/phonon/mode", params={"q": 0, "band": 0}).json()["frequencies"])
        check("フォノン: Γ 点の音響モードは 0、光学モードは 10 THz 以上", max(abs(v) for v in g[:3]) < 0.2 and g[3] > 10,
              [round(v, 2) for v in g])

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
            page.goto(f"{BASE}/#/md?structure={cu}")
            expect(page.get_by_role("heading", name="温度・圧力の流れ")).to_be_visible(timeout=15000)
            note = page.get_by_text("SevenNet (GNN 系の汎用力場) は、GPUMD ではなく ASE で計算します", exact=False)
            check("画面: NEP のときは SevenNet の注意が出ない", not note.is_visible())
            page.locator("label.field", has_text="ポテンシャル").locator("select").first.select_option(label="SevenNet-0 (7net-0、速い)")
            expect(note).to_be_visible(timeout=5000)
            check("画面: SevenNet を選ぶと、ASE で計算する注意が出る", note.is_visible())
            page.screenshot(path=SHOTS / "s01_md_sevennet.png", full_page=True)
            page.goto(f"{BASE}/#/jobs/{job_md}")
            expect(page.get_by_role("heading", name="熱力学量の時間推移")).to_be_visible(timeout=15000)
            page.wait_for_timeout(2000)
            page.screenshot(path=SHOTS / "s02_md_sevennet_done.png", full_page=True)
            page.goto(f"{BASE}/#/jobs/{phonon}")
            expect(page.get_by_role("heading", name="振動のようす")).to_be_visible(timeout=20000)
            page.wait_for_timeout(1500)
            page.screenshot(path=SHOTS / "s03_phonon_sevennet.png", full_page=True)
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
