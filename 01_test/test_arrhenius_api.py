"""温度スキャンとアレニウス解析の通しテスト。テスト用のサーバーを立てて、液体 Cu (108 原子・NEP89) を
4 温度 (NPT) で短く流し、シリーズの作成 → 完了 → 当てはめ → 点の除外 → csv を確認する。

    ~/.venv/gpumd312/bin/python 01_test/test_arrhenius_api.py
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_arrhenius"))
PORT = 8024
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
TEMPS = [2400, 2700, 3000, 3300]   # NPT でどれも確実に液体になる温度
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)
    log = open(DATA.parent / "test_arrhenius_server.log", "w")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "matsimapp.main:app", "--port", str(PORT)],
                              cwd=ROOT, env={**os.environ, "MATSIM_DATA_DIR": str(DATA)}, stdout=log,
                              stderr=subprocess.STDOUT)
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
        potential = s.get(f"{BASE}/api/potentials").json()[0]["id"]
        body = {"kind": "diffusion", "structure_id": cu, "potential_id": potential, "repeat": [3, 3, 3],
                "time_step": 1.0, "thermo_interval": 100, "traj_interval": 0,
                "options": {"temperature": 1000, "scan_temperatures": ", ".join(map(str, TEMPS)), "ensemble": "npt",
                            "pressure": 0, "equil_steps": 4000, "steps": 8000, "ic_element": "Cu", "ic_charge": 1, "rdf": False}}

        r = s.post(f"{BASE}/api/calc/preview", json=body)
        check("温度スキャンのプレビュー", r.ok and r.json().get("scan") == TEMPS, r.json().get("scan") or r.text[:100])
        bad = {**body, "options": {**body["options"], "scan_temperatures": "1500"}}
        r = s.post(f"{BASE}/api/calc/preview", json=bad)
        check("温度が 1 つだけならエラー", r.status_code == 400, r.json().get("detail"))
        bad = {**body, "options": {**body["options"], "scan_temperatures": "1500,abc"}}
        check("書き方の誤りはエラー", s.post(f"{BASE}/api/calc/preview", json=bad).status_code == 400)

        r = s.post(f"{BASE}/api/calc/jobs", json=body).json()
        ids, series = r.get("ids", []), r.get("series")
        check("温度ごとにジョブを作る", len(ids) == len(TEMPS) and series, f"{ids} {series}")
        listing = {j["id"]: j for j in s.get(f"{BASE}/api/jobs").json()}
        check("同じシリーズ", all(listing[i]["series"] == series for i in ids))
        check("ジョブごとの温度", [listing[i]["summary"].get("temperature") for i in ids] == TEMPS)
        detail = s.get(f"{BASE}/api/jobs/{ids[0]}").json()
        check("ジョブの温度が置き換わる", detail["params"]["options"]["temperature"] == TEMPS[0]
              and detail["params"]["options"]["scan_temperatures"] == "")

        start = time.time()
        while time.time() - start < 600:
            states = [s.get(f"{BASE}/api/jobs/{i}").json()["status"] for i in ids]
            if all(st in ("done", "failed", "cancelled") for st in states):
                break
            time.sleep(2)
        check("全温度が完了", all(st == "done" for st in states), f"{states} {time.time() - start:.0f}s")

        q = f"jobs={','.join(map(str, ids))}"
        a = s.get(f"{BASE}/api/arrhenius?{q}").json()
        names = [t["name"] for t in a["targets"]]
        check("D と σ を当てはめる", "D:Cu" in names and "sigma:Cu" in names, names)
        d = next(t for t in a["targets"] if t["name"] == "D:Cu")
        fit = d["fit"] or {}
        check("活性化エネルギー (液体 Cu なので 0.1〜1 eV 程度)", fit and 0.05 < fit["ea_ev"] < 1.5,
              f"Ea={fit.get('ea_ev')} ± {fit.get('ea_err')} R²={fit.get('r2')}")
        check("液体なので全点を使う", all(p["used"] and not p["slow"] for p in d["points"]),
              [(round(p["temperature"]), p["value"]) for p in d["points"]])
        temps = [p["temperature"] for p in d["points"]]
        check("実測の平均温度を使う", all(abs(t - s_) < 150 for t, s_ in zip(temps, TEMPS)) and temps != TEMPS, temps)
        a2 = s.get(f"{BASE}/api/arrhenius?{q}&temperature=set").json()
        check("設定温度も選べる", [p["temperature"] for p in a2["targets"][1]["points"]] == TEMPS)
        a3 = s.get(f"{BASE}/api/arrhenius?{q}&excluded={ids[-1]}").json()
        d3 = next(t for t in a3["targets"] if t["name"] == "D:Cu")
        check("点を外して当てはめ直す", d3["fit"]["n_points"] == 3 and not d3["points"][-1]["used"])
        sigma = next(t for t in a["targets"] if t["name"] == "sigma:Cu")
        check("σ の当てはめ (σT)", sigma["fit"] and sigma["fit"]["ea_ev"] > 0, sigma["fit"] and sigma["fit"]["ea_ev"])
        r = s.get(f"{BASE}/api/arrhenius/csv?{q}")
        check("csv", r.ok and "Ea_eV" in r.text and r.headers["content-type"].startswith("text/csv"))
        check("ジョブなしはエラー", s.get(f"{BASE}/api/arrhenius").status_code == 400)
        check("不正な条件はエラー", s.get(f"{BASE}/api/arrhenius?{q}&temperature=abc").status_code == 400)
    finally:
        server.terminate()
        server.wait(timeout=10)
        if os.environ.get("KEEP_DATA") != "1":
            shutil.rmtree(DATA, ignore_errors=True)
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} OK" + (f"  失敗: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
