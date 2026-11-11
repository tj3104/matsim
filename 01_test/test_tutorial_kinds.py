"""GPUMD-Tutorials 由来の計算の種類の通しテスト。テスト用のサーバーを立てて、小さな系・短いステップで
フォノン分散・振動状態密度・NEMD/HNEMD (スペクトル)・熱膨張・融点 (温度スキャン)・有限温度の弾性定数・疲労・
衝突・成膜・表面・サイトエネルギー分布 を GPUMD で流し、結果の要点とグラフが取り出せるかを確かめる。

    KEEP_DATA=1 MATSIM_TEST_DATA=<dir> ~/.venv/gpumd312/bin/python 01_test/test_tutorial_kinds.py
"""
import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
from ase.build import bulk
from ase.io import write

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_tutorial"))
PORT = 8026
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "matsimapp.main:app", "--port", str(PORT)],
                              cwd=ROOT, env={**os.environ, "MATSIM_DATA_DIR": str(DATA)},
                              stdout=open(DATA.parent / "test_tutorial_server.log", "w"), stderr=subprocess.STDOUT)
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
        potential = s.get(f"{BASE}/api/potentials").json()[0]["id"]
        cu = s.post(f"{BASE}/api/structures/sample", json={"key": "Cu"}).json()["id"]
        buf = io.StringIO()
        write(buf, bulk("Si", "diamond", a=5.43), format="vasp", direct=True)
        si = s.post(f"{BASE}/api/structures/upload", files={"file": ("POSCAR", buf.getvalue())},
                    data={"name": "Si prim"}).json()["id"]
        big = s.post(f"{BASE}/api/modeling/save", json={"op": "supercell", "structure_id": cu,
                                                         "options": {"nx": 4, "ny": 4, "nz": 6}}).json()["id"]
        slab = s.post(f"{BASE}/api/modeling/save", json={"op": "vacuum", "structure_id": big,
                                                          "options": {"axis": "z", "vacuum": 25, "center": True}}).json()["id"]
        kinds = {k["key"]: k for k in s.get(f"{BASE}/api/kinds").json()["kinds"]}
        for key in ("phonon", "vdos", "expansion", "melting", "elastic_t", "fatigue", "impact", "deposition", "surface", "doas"):
            check(f"種類がある: {key}", key in kinds)

        def job(kind, structure, options, repeat=(1, 1, 1), **extra):
            body = {"kind": kind, "structure_id": structure, "potential_id": potential, "repeat": list(repeat),
                    "time_step": 1.0, "thermo_interval": 50, "traj_interval": 0, "options": options, **extra}
            r = s.post(f"{BASE}/api/calc/preview", json=body)
            if not r.ok:
                check(f"プレビュー {kind}", False, r.text[:200])
                return None
            r = s.post(f"{BASE}/api/calc/jobs", json=body)
            if not r.ok:
                check(f"投入 {kind}", False, r.text[:200])
                return None
            return r.json()

        submitted = {
            "phonon": job("phonon", si, {}),
            "vdos": job("vdos", cu, {"equil_steps": 2000, "steps": 4000}, (4, 4, 4)),
            "nemd": job("thermal", cu, {"method": "nemd", "direction": "z", "equil_steps": 2000, "steps": 6000,
                                        "n_blocks": 6, "delta_T": 20, "spectral": True}, (3, 3, 14)),
            "hnemd": job("thermal", cu, {"method": "hnemd", "equil_steps": 2000, "steps": 4000, "spectral": True,
                                         "output_interval": 500}, (4, 4, 4)),
            "expansion": job("expansion", cu, {"temperatures": "300, 600, 900", "steps": 3000}, (4, 4, 4)),
            "melting": job("melting", cu, {"scan_temperatures": "900, 2200", "equil_steps": 2000, "melt_steps": 3000,
                                           "steps": 6000, "axis": "z"}, (4, 4, 10)),
            "elastic_t": job("elastic_t", cu, {"equil_steps": 2000, "steps": 20000, "sample_interval": 10}, (4, 4, 4)),
            "fatigue": job("fatigue", cu, {"amplitude": 2, "strain_rate": 1e10, "cycles": 2, "equil_steps": 2000},
                           (4, 4, 4)),
            "impact": job("impact", slab, {"velocity": 0.5, "projectile": 5, "fixed": 3, "equil_steps": 1000,
                                           "steps": 1500}),
            "deposition": job("deposition", slab, {"species": "Cu:2", "interval": 500, "rounds": 4}),
            "surface": job("surface", slab, {"steps": 3000, "temperature": 600}),
            "doas": job("doas", cu, {"element": "Cu", "equil_steps": 1000, "steps": 3000, "dump_interval": 200},
                        (4, 4, 4)),
        }
        ids = {name: r["ids"] for name, r in submitted.items() if r}
        all_ids = [i for v in ids.values() for i in v]
        start = time.time()
        while time.time() - start < 1500:
            states = {i: s.get(f"{BASE}/api/jobs/{i}").json()["status"] for i in all_ids}
            if all(st in ("done", "failed", "cancelled") for st in states.values()):
                break
            time.sleep(3)
        print(f"実行時間 {time.time() - start:.0f}s", flush=True)

        expect = {"phonon": "dispersion", "vdos": "dos", "nemd": "profile", "hnemd": "shc", "expansion": "expansion",
                  "melting": "coexistence", "elastic_t": "elastic_t", "fatigue": "fatigue", "impact": "impact_t",
                  "deposition": "profile", "surface": "cn", "doas": "aedp"}
        for name, job_ids in ids.items():
            for i in job_ids:
                detail = s.get(f"{BASE}/api/jobs/{i}").json()
                if detail["status"] != "done":
                    check(f"{name} #{i} 完了", False, detail.get("message", "")[:200])
                    continue
                res = s.get(f"{BASE}/api/jobs/{i}/results").json()
                keys = [d["key"] for d in res["datasets"]]
                failed_notes = [n for n in res["notes"] if "読めませんでした" in n]
                highlights = {h["label"]: h["value"] for h in res["highlights"]}
                check(f"{name} #{i} 結果", expect[name] in keys and not failed_notes,
                      f"{list(highlights.items())[:4]} {failed_notes}")
        crush = {"kind": "impact", "structure_id": slab, "potential_id": potential, "repeat": [1, 1, 1],
                 "time_step": 1.0, "thermo_interval": 50, "traj_interval": 0,
                 "options": {"velocity": 5, "projectile": 5, "fixed": 3, "steps": 20000}}
        r = s.post(f"{BASE}/api/calc/preview", json=crush)
        check("衝突: 標的を押しつぶす条件はエラー", r.status_code == 400, r.json().get("detail", "")[:60])
        if "melting" in ids:
            series = submitted["melting"]["series"]
            summary = s.get(f"{BASE}/api/series/{series}").json()
            check("融点シリーズのまとめ", summary["estimate"] is not None, summary["estimate"])
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
