"""ウォーレン・カウリーの秩序度 (短範囲秩序) と配位多面体の通しテスト。
test_transition.py を KEEP_DATA=1 で流して残したデータ (Ti を昇温したジョブ) に、答えの分かる構造を足して確かめる:
  短範囲秩序: ランダム合金 (α≈0)・B2 (第 1 殻 −1、第 2 殻 +1)・L1₂ (Au–Cu −1/3)・相分離 (α>0)・1 元素はエラー
  配位多面体: MgO (八面体・辺共有)・SrTiO3 (八面体・頂点共有・6 個)・β クリストバライト (四面体・Q4)・
              hcp の Ti (反立方八面体) と、その昇温のジョブの時間変化
短い MD (CuNi のランダム合金) で短範囲秩序の時間変化も出す。画面はスマホ幅で確かめる。

    MATSIM_TEST_DATA=<dir> CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_order.py
"""
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import requests
from ase import Atoms
from ase.build import bulk
from ase.io import write
from ase.neighborlist import neighbor_list
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_transition"))
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8040
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def structures():
    rng = np.random.default_rng(1)
    alloy = bulk("Cu", "fcc", a=3.6, cubic=True).repeat(6)
    s = np.array(alloy.get_chemical_symbols())
    s[rng.permutation(len(alloy))[:len(alloy) // 2]] = "Ni"
    alloy.set_chemical_symbols(s)
    b2 = Atoms("CuZn", scaled_positions=[[0, 0, 0], [.5, .5, .5]], cell=[2.95] * 3, pbc=True).repeat(6)
    l12 = Atoms("AuCu3", scaled_positions=[[0, 0, 0], [.5, .5, 0], [.5, 0, .5], [0, .5, .5]], cell=[3.75] * 3, pbc=True).repeat(5)
    sep = bulk("Cu", "fcc", a=3.6, cubic=True).repeat((10, 5, 5))
    sep.set_chemical_symbols(np.where(sep.get_scaled_positions()[:, 0] < 0.5, "Cu", "Ni"))
    sto = Atoms("SrTiO3", scaled_positions=[[0, 0, 0], [.5, .5, .5], [.5, .5, 0], [.5, 0, .5], [0, .5, .5]],
                cell=[3.905] * 3, pbc=True).repeat(3)
    si = bulk("Si", "diamond", a=7.16, cubic=True)
    i, j, D = neighbor_list("ijD", si, 3.2)
    mids = np.array([si.positions[a] + D[k] / 2 for k, a in enumerate(i)])
    frac = np.unique(np.mod(np.linalg.solve(si.cell.array.T, mids.T).T, 1.0).round(4) % 1.0, axis=0)
    crist = (si + Atoms(["O"] * len(frac), scaled_positions=frac, cell=si.cell, pbc=True)).repeat(2)
    return {"CuNi ランダム": alloy, "CuZn B2": b2, "Cu3Au L12": l12, "CuNi 相分離": sep, "SrTiO3": sto,
            "SiO2 クリストバライト": crist}


def upload(s, atoms, name):
    buf = io.StringIO()
    write(buf, atoms, format="vasp", direct=True, sort=False)
    r = s.post(f"{BASE}/api/structures/upload", files={"file": ("POSCAR", buf.getvalue())}, data={"name": name})
    return r.json()["id"]


def analyze(s, kind, body, timeout=300):
    r = s.post(f"{BASE}/api/analysis/{kind}", json=body)
    if not r.ok:
        return r, None
    task = r.json()["id"]
    start = time.time()
    while time.time() - start < timeout:
        data = s.get(f"{BASE}/api/jobs/{task}/{kind}").json()
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
        have = {x["name"]: x["id"] for x in s.get(f"{BASE}/api/structures").json()}
        ids = {name: have.get(name) or upload(s, atoms, name) for name, atoms in structures().items()}
        if "MgO (岩塩型)" not in have:
            have["MgO (岩塩型)"] = s.post(f"{BASE}/api/structures/sample", json={"key": "MgO"}).json()["id"]
        ti_job = next(j["id"] for j in s.get(f"{BASE}/api/jobs").json()
                      if j["kind"] == "transition" and j["status"] == "done" and j["name"].startswith("Ti"))

        # ---- 短範囲秩序 ------------------------------------------------------------------------
        def alpha(name, a, b, shell=0):
            _, data = analyze(s, "sro", {"structure_id": ids[name]})
            sh = data["result"]["shells"][shell]
            return sh["alpha"][a][b], sh["sigma"][a][b], data["result"]
        a, sg, _ = alpha("CuNi ランダム", "Cu", "Ni")
        check("ランダム合金: α ≈ 0 (ゆらぎ 3σ 以内)", abs(a) < 3 * sg, (round(a, 3), round(sg, 3)))
        a1, _, res = alpha("CuZn B2", "Cu", "Zn")
        a2 = res["shells"][1]["alpha"]["Cu"]["Zn"]
        check("B2: 第 1 殻 −1、第 2 殻 +1", abs(a1 + 1) < 0.02 and abs(a2 - 1) < 0.02, (a1, a2))
        a, _, _ = alpha("Cu3Au L12", "Au", "Cu")
        check("L1₂: Au のまわりの Cu は −1/3", abs(a + 1 / 3) < 0.02, a)
        a, sg, _ = alpha("CuNi 相分離", "Cu", "Ni")
        check("相分離: 違う元素の α が正 (同じ元素が集まる)", a > 0.5 and a > 3 * sg, a)
        _, data = analyze(s, "sro", {"structure_id": have["Ti hcp"]})
        check("1 元素の構造は失敗し、理由が出る", data["job"]["status"] == "failed" and "2 種類" in data["job"]["message"],
              data["job"]["message"])

        # ---- 配位多面体 ------------------------------------------------------------------------
        def poly(sid, **kw):
            _, data = analyze(s, "poly", {"structure_id": sid, **kw})
            return data["result"]
        r = poly(have["MgO (岩塩型)"])
        check("MgO: Mg–O の八面体・辺共有", r["center"] == "Mg" and r["ligand"] == "O" and r["types"][0]["name"] == "八面体"
              and r["sharing"]["辺共有"] > 0, (r["types"][0]["name"], r["sharing"]))
        r = poly(ids["SrTiO3"], center="Ti")
        check("SrTiO3: TiO6 八面体・頂点共有で 6 個とつながる", r["types"][0]["name"] == "八面体"
              and r["sharing"]["辺共有"] == 0 and r["qn"][0]["n"] == 6, (r["sharing"], r["qn"]))
        r = poly(ids["SiO2 クリストバライト"])
        check("SiO2: SiO4 四面体・頂点共有・Q4", r["types"][0]["name"] == "四面体" and r["qn"][0]["n"] == 4
              and r["polyhedra"][0]["faces"] and len(r["polyhedra"][0]["faces"]) == 4, r["qn"])
        r = poly(have["Ti hcp"])
        check("hcp の Ti: 反立方八面体", r["types"][0]["name"] == "反立方八面体 (hcp)", r["types"][:2])
        _, data = analyze(s, "poly", {"job_id": ti_job, "frames": 30})
        evo = data["result"].get("evolution", {})
        check("Ti の昇温のジョブ: 形の割合の時間変化", len(evo.get("time_ps", [])) >= 20
              and "反立方八面体 (hcp)" in evo.get("fractions", {}), list(evo.get("fractions", {})))
        r, _ = analyze(s, "poly", {"structure_id": ids["SrTiO3"], "center": "Zr"})
        _, data = analyze(s, "poly", {"structure_id": ids["SrTiO3"], "center": "Zr"})
        check("ない元素は失敗し、理由が出る", data["job"]["status"] == "failed" and "Zr" in data["job"]["message"])

        # ---- 短範囲秩序の時間変化 (短い MD) ---------------------------------------------------------
        potential = s.get(f"{BASE}/api/potentials").json()[0]["id"]
        md = s.post(f"{BASE}/api/md/jobs", json={"name": "CuNi alloy", "structure_id": ids["CuNi ランダム"],
                    "potential_id": potential, "repeat": [1, 1, 1], "time_step": 2.0, "minimize": False,
                    "thermo_interval": 100, "traj_interval": 200,
                    "stages": [{"ensemble": "nvt", "steps": 4000, "temperature": 600}]}).json()["id"]
        start = time.time()
        while time.time() - start < 600 and s.get(f"{BASE}/api/jobs/{md}").json()["status"] not in ("done", "failed"):
            time.sleep(3)
        _, data = analyze(s, "sro", {"job_id": md, "frames": 15})
        evo = data["result"].get("evolution", {})
        check("MD のジョブ: 第 1 殻の α の時間変化", len(evo.get("time_ps", [])) >= 10 and "Cu–Ni" in evo.get("alpha", {}),
              [round(v, 3) for v in evo.get("alpha", {}).get("Cu–Ni", [])[:4]])

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
            # 短範囲秩序 (B2)
            page.goto(f"{BASE}/#/structures/{ids['CuZn B2']}")
            page.get_by_role("link", name="短範囲秩序", exact=True).click()
            page.get_by_role("button", name="解析を始める").click()
            expect(page.get_by_role("heading", name="ウォーレン・カウリーの秩序度")).to_be_visible(timeout=60000)
            page.wait_for_timeout(2500)
            cells = page.locator("td.sro-cell .sro-value").all_inner_texts()
            check("画面: α の表 (B2 の第 1 殻に −1.000)", "-1.000" in cells, cells)
            page.get_by_role("button", name="第 2 殻").click()
            page.wait_for_timeout(500)
            check("画面: 殻を切り替える (第 2 殻は +1.000)", "+1.000" in page.locator("td.sro-cell .sro-value").all_inner_texts())
            check("画面: null が出ない (短範囲秩序)", "null" not in page.locator("body").inner_text())
            page.screenshot(path=SHOTS / "o01_sro_b2.png", full_page=True)
            # 配位多面体 (クリストバライト)
            page.goto(f"{BASE}/#/structures/{ids['SiO2 クリストバライト']}")
            page.get_by_role("link", name="配位多面体", exact=True).click()
            page.get_by_role("button", name="解析を始める").click()
            expect(page.get_by_role("heading", name="配位多面体の形")).to_be_visible(timeout=60000)
            page.wait_for_timeout(2500)
            check("画面: 形の表に四面体", "四面体" in page.locator("table").first.inner_text())
            check("画面: 多面体を描いている", "個の多面体を描いています" in str(page.locator("p.note").all_inner_texts()))
            from PIL import Image
            page.locator("canvas.viewer").screenshot(path=SHOTS / "o03_poly_canvas.png")
            pixels = list(Image.open(SHOTS / "o03_poly_canvas.png").convert("RGB").getdata())
            blue = sum(1 for r, g, b in pixels if b > r + 40 and b > g + 10)
            check("画面: 3D 表示に四面体 (青) が描かれる", blue > 1000, blue)
            check("画面: null が出ない (配位多面体)", "null" not in page.locator("body").inner_text())
            page.screenshot(path=SHOTS / "o02_poly_sio2.png", full_page=True)
            page.goto(f"{BASE}/#/structures/{ids['SrTiO3']}")
            page.get_by_role("link", name="配位多面体", exact=True).click()
            page.locator("label.field", has_text="中心の元素").locator("select").select_option(label="自動")
            page.get_by_role("button", name="解析を始める").click()
            expect(page.get_by_role("heading", name="配位多面体の形")).to_be_visible(timeout=60000)
            page.locator("label.field", has_text="中心の元素").locator("select").select_option("Ti")
            page.get_by_role("button", name="この条件で解析し直す").click()
            expect(page.get_by_text("Ti – O")).to_be_visible(timeout=60000)
            page.wait_for_timeout(2000)
            page.locator("canvas.viewer").screenshot(path=SHOTS / "o04_poly_perovskite.png")
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
