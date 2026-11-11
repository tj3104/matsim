"""モデリング API の通しテスト。テスト用のデータ置き場とポートで別のサーバーを立てて、
一覧 → プレビュー → 保存 (原子の編集・結晶の作成・別の構造を使う操作・STL) を確認する。

    ~/.venv/gpumd312/bin/python 01_test/test_modeling_api.py
"""
import base64
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_modeling_api"))
PORT = 8022
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    env = {**os.environ, "MATSIM_DATA_DIR": str(DATA)}
    log = open(DATA.parent / "test_modeling_server.log", "w")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "matsimapp.main:app", "--port", str(PORT)],
                              cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                requests.get(f"{BASE}/api/status", timeout=1)
                break
            except requests.ConnectionError:
                time.sleep(0.5)
        s = requests.Session()
        s.headers.update(H)
        r = s.post(f"{BASE}/api/auth/setup", json={"username": "tester", "password": "test-password-123"})
        check("アカウント作成", r.ok, r.text[:100])

        listing = s.get(f"{BASE}/api/modeling").json()
        check("操作の一覧", len(listing["ops"]) >= 30 and listing["atomsk"], f"{len(listing['ops'])} 種類")
        check("計算環境に Atomsk", s.get(f"{BASE}/api/system").json()["atomsk"]["available"])

        cu = s.post(f"{BASE}/api/structures/sample", json={"key": "Cu"}).json()["id"]
        mgo = s.post(f"{BASE}/api/structures/sample", json={"key": "MgO"}).json()["id"]

        body = {"op": "supercell", "structure_id": cu, "options": {"nx": 3, "ny": 3, "nz": 3}}
        p = s.post(f"{BASE}/api/modeling/preview", json=body).json()
        check("スーパーセルのプレビュー", p.get("n_atoms") == 108, p.get("formula"))
        big = s.post(f"{BASE}/api/modeling/save", json={**body, "name": "Cu 3x3x3"}).json()["id"]

        body = {"op": "substitute", "structure_id": big,
                "options": {"from": "Cu", "to": "Ni", "amount": "percent", "percent": 50, "seed": 7}}
        p1 = s.post(f"{BASE}/api/modeling/preview", json=body).json()
        p2 = s.post(f"{BASE}/api/modeling/preview", json=body).json()
        check("置換 50%", p1.get("formula") == "Cu54Ni54", p1.get("formula"))
        check("同じシードなら同じ構造", p1["view"]["symbols"] == p2["view"]["symbols"])
        saved = s.post(f"{BASE}/api/modeling/save", json=body).json()["id"]
        info = s.get(f"{BASE}/api/structures/{saved}").json()
        check("置換の保存", info["source"] == "model" and info["formula"] == "Cu54Ni54", info["source_detail"][:60])

        r = s.post(f"{BASE}/api/modeling/preview", json={"op": "vacancy", "structure_id": big,
                                                          "options": {"amount": "count", "count": 5}})
        check("空孔 5 個", r.ok and r.json()["n_atoms"] == 103)

        r = s.post(f"{BASE}/api/modeling/preview", json={"op": "random_alloy", "structure_id": big,
                                                          "options": {"composition": "Cu:1,Ni:1,Co:1,Fe:1"}})
        check("ランダム合金", r.ok and r.json()["formula"] == "Co27Cu27Fe27Ni27", r.json().get("formula"))

        r = s.post(f"{BASE}/api/modeling/preview", json={"op": "substitute", "structure_id": big,
                                                          "options": {"from": "Mg", "to": "Ni"}})
        check("ない元素の置換はエラー", r.status_code == 400, r.json().get("detail"))

        r = s.post(f"{BASE}/api/modeling/preview", json={"op": "create", "options": {
            "lattice": "rocksalt", "a": 5.64, "elements": "Na Cl", "nx": 2, "ny": 2, "nz": 2}})
        check("結晶を作る (NaCl)", r.ok and sorted(r.json()["formula"]) == sorted("Na32Cl32"), r.json().get("formula") or r.text[:80])

        r = s.post(f"{BASE}/api/modeling/preview", json={"op": "create", "options": {"lattice": "perovskite", "elements": "Cu"}})
        check("元素の数が違うとエラー", r.status_code == 400, r.json().get("detail"))

        r = s.post(f"{BASE}/api/modeling/preview", json={"op": "merge", "structure_id": big,
                                                          "options": {"other": mgo, "axis": "z"}})
        check("2 つの構造を重ねる", r.ok and "Mg" in r.json()["formula"], r.json().get("formula") or r.text[:80])

        r = s.post(f"{BASE}/api/modeling/preview", json={"op": "merge", "structure_id": big, "options": {"other": 9999}})
        check("ない構造を選ぶとエラー", r.status_code == 400, r.json().get("detail"))

        stl = ("solid c\n" + "".join("facet normal 0 0 0\nouter loop\n" + "".join(f"vertex {x} {y} {z}\n" for x, y, z in t)
               + "endloop\nendfacet\n" for t in [
                   [(0, 0, 0), (1, 1, 0), (1, 0, 0)], [(0, 0, 0), (0, 1, 0), (1, 1, 0)], [(0, 0, 1), (1, 0, 1), (1, 1, 1)],
                   [(0, 0, 1), (1, 1, 1), (0, 1, 1)], [(0, 0, 0), (1, 0, 0), (1, 0, 1)], [(0, 0, 0), (1, 0, 1), (0, 0, 1)],
                   [(0, 1, 0), (1, 1, 1), (1, 1, 0)], [(0, 1, 0), (0, 1, 1), (1, 1, 1)], [(0, 0, 0), (0, 0, 1), (0, 1, 1)],
                   [(0, 0, 0), (0, 1, 1), (0, 1, 0)], [(1, 0, 0), (1, 1, 0), (1, 1, 1)], [(1, 0, 0), (1, 1, 1), (1, 0, 1)]])
               + "endsolid c\n")
        r = s.post(f"{BASE}/api/modeling/preview", json={"op": "stl", "structure_id": big, "options": {
            "stl": {"name": "c.stl", "data": base64.b64encode(stl.encode()).decode()}, "placement": "rescale"}})
        check("STL で切り抜く", r.ok and 0 < r.json()["n_atoms"] < 108, r.json().get("n_atoms") or r.text[:80])

        start = time.time()
        body = {"op": "granular", "structure_id": cu, "options": {"lx": 100, "ly": 100, "lz": 15, "grains": 6, "radius": 15}}
        r = s.post(f"{BASE}/api/modeling/preview", json=body)
        check("グラニュラー構造", r.ok and r.json()["n_atoms"] > 5000,
              f"{r.json().get('n_atoms')} 原子 {time.time() - start:.1f}s" if r.ok else r.text[:100])
        start = time.time()
        g = s.post(f"{BASE}/api/modeling/save", json=body).json()["id"]
        check("プレビューを使い回して保存", time.time() - start < 1.0, f"{time.time() - start:.2f}s")
        check("保存した構造を開ける", s.get(f"{BASE}/api/structures/{g}").ok)

        r = s.post(f"{BASE}/api/modeling/preview", json={"op": "polycrystal", "structure_id": cu,
                                                          "options": {"lx": 40, "ly": 40, "lz": 40, "grains": 4, "matrix": mgo, "clearance": 3}})
        check("多結晶 (粒界相あり)", r.ok and "Mg" in r.json()["formula"], r.json().get("formula") or r.text[:100])
    finally:
        server.terminate()
        server.wait(timeout=10)
        shutil.rmtree(DATA, ignore_errors=True)
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} OK" + (f"  失敗: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
