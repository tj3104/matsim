"""API の通しテスト。テスト用のデータ置き場とポートで別のサーバーを立てて、
アカウント作成 → 構造アップロード → プレビュー → MD 投入 → 完了 → 結果の読み出し → 中止・削除 を確認する。

    ~/.venv/gpumd312/bin/python 01_test/test_api.py
"""
import io
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import requests
from ase.build import bulk
from ase.io import write

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "01_test" / "data_api"
PORT = 8021
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def wait_status(s, job_id, wanted, timeout=180):
    start = time.time()
    while time.time() - start < timeout:
        job = s.get(f"{BASE}/api/jobs/{job_id}").json()
        if job["status"] in wanted:
            return job
        time.sleep(0.5)
    raise TimeoutError(f"job {job_id} が {wanted} になりません: {job['status']}")


def md_params(structure_id, potential_id, **over):
    params = {"name": "", "structure_id": structure_id, "potential_id": potential_id, "repeat": [4, 4, 4],
              "time_step": 1.0, "minimize": False, "thermo_interval": 50, "traj_interval": 200,
              "stages": [{"ensemble": "nvt", "steps": 1000, "temperature": 300},
                         {"ensemble": "npt", "steps": 1000, "temperature": 300, "temperature_end": 600, "pressure": 0}]}
    params.update(over)
    return params


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    env = {**os.environ, "MATSIM_DATA_DIR": str(DATA)}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "matsimapp.main:app", "--port", str(PORT)],
                              cwd=ROOT, env=env, stdout=open(ROOT / "01_test" / "test_server.log", "w"),
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

        # --- 認証 ---
        check("最初はセットアップが必要", s.get(f"{BASE}/api/status").json()["needs_setup"] is True)
        check("未ログインでは構造一覧を見られない", s.get(f"{BASE}/api/structures").status_code == 401)
        check("未ログインではジョブを投入できない", s.post(f"{BASE}/api/md/jobs", json={}).status_code == 401)
        check("短いパスワードは拒否", s.post(f"{BASE}/api/auth/setup", json={"username": "tester", "password": "short"}).status_code == 422)
        r = s.post(f"{BASE}/api/auth/setup", json={"username": "tester", "password": "test-password-123"})
        check("最初のアカウントを作成", r.status_code == 200, r.text[:80])
        check("2 つ目のセットアップは拒否", requests.post(f"{BASE}/api/auth/setup", headers=H, json={"username": "other", "password": "test-password-123"}).status_code == 403)
        check("CSRF ヘッダーなしの変更は拒否", requests.post(f"{BASE}/api/auth/logout", cookies=s.cookies).status_code == 403)
        check("間違ったパスワードは拒否", requests.post(f"{BASE}/api/auth/login", headers=H, json={"username": "tester", "password": "wrong-password"}).status_code == 401)
        check("ログイン中の情報", s.get(f"{BASE}/api/me").json()["username"] == "tester")
        system = s.get(f"{BASE}/api/system").json()
        check("GPUMD と GPU を認識", system["gpumd"]["available"] and system["gpu"]["available"], str(system["gpumd"]))

        # --- 構造 ---
        buf = io.BytesIO()
        write(buf, bulk("Cu", cubic=True), format="cif")
        r = s.post(f"{BASE}/api/structures/upload", files={"file": ("Cu.cif", buf.getvalue())}, data={"name": "Cu fcc"})
        check("cif をアップロード", r.status_code == 200, r.text[:100])
        cu = r.json()["id"]
        buf = io.StringIO()
        write(buf, bulk("Si", cubic=True), format="vasp")
        r = s.post(f"{BASE}/api/structures/upload", files={"file": ("POSCAR", buf.getvalue().encode())})
        check("POSCAR をアップロード", r.status_code == 200, r.text[:100])
        r = s.post(f"{BASE}/api/structures/upload", files={"file": ("evil.cif", b"this is not a cif\x00\x01")})
        check("壊れたファイルは 400", r.status_code == 400, r.text[:100])
        r = s.post(f"{BASE}/api/structures/upload", files={"file": ("a.exe", b"MZ")})
        check("対応外の拡張子は 400", r.status_code == 400)
        r = s.post(f"{BASE}/api/structures/upload", files={"file": ("mol.xyz", b"2\n\nH 0 0 0\nH 0 0 0.74\n")})
        check("セルのない xyz は 400", r.status_code == 400, r.json().get("detail", "")[:40])
        detail = s.get(f"{BASE}/api/structures/{cu}", params={"na": 3, "nb": 3, "nc": 3}).json()
        check("スーパーセルの原子数 4×27=108", detail["repeat"]["n_atoms"] == 108 and len(detail["view"]["symbols"]) == 108)
        samples = {x["key"] for x in s.get(f"{BASE}/api/structures/samples").json()}
        mgo = s.post(f"{BASE}/api/structures/sample", json={"key": "MgO"})
        info = s.get(f"{BASE}/api/structures/{mgo.json()['id']}").json()
        check("テスト構造 MgO を追加", samples == {"Cu", "MgO"} and info["n_atoms"] == 8 and info["info"]["species"] == {"Mg": 4, "O": 4} and abs(info["info"]["a"] - 4.212) < 1e-6, info["formula"])
        r = s.post(f"{BASE}/api/md/preview", json=md_params(mgo.json()["id"], s.get(f"{BASE}/api/potentials").json()[0]["id"], repeat=[3, 3, 3]))
        check("MgO を NEP89 で計算できる (プレビュー)", r.status_code == 200 and r.json()["n_atoms"] == 216, r.text[:80])
        cu_sample = s.post(f"{BASE}/api/structures/sample", json={"key": "Cu"})
        check("テスト構造 Cu を追加", cu_sample.status_code == 200 and s.get(f"{BASE}/api/structures/{cu_sample.json()['id']}").json()["n_atoms"] == 4)
        check("存在しないテスト構造は 400", s.post(f"{BASE}/api/structures/sample", json={"key": "Xx"}).status_code == 400)
        s.delete(f"{BASE}/api/structures/{mgo.json()['id']}")
        s.delete(f"{BASE}/api/structures/{cu_sample.json()['id']}")
        check("構造一覧は 2 件", len(s.get(f"{BASE}/api/structures").json()) == 2)

        # --- ポテンシャル ---
        pots = s.get(f"{BASE}/api/potentials").json()
        check("NEP89 が登録済み", pots and pots[0]["builtin"] and "Cu" in pots[0]["elements"], pots[0]["name"] if pots else "")
        nep = pots[0]["id"]
        # 単斜晶 (セルの角度が 90° から少しずれた構造) のフォノン: 高対称点に数字の付く名前 (M1・H1 など) がある
        mono = bulk("Mg", "hcp", orthorhombic=True).repeat((3, 2, 3))
        cell = mono.cell.array.copy()
        cell[2, 0] += 0.03
        mono.set_cell(cell, scale_atoms=True)
        buf = io.StringIO()
        write(buf, mono, format="vasp")
        mg = s.post(f"{BASE}/api/structures/upload", files={"file": ("POSCAR", buf.getvalue().encode())},
                    data={"name": "Mg 単斜晶"}).json()["id"]
        r = s.post(f"{BASE}/api/calc/preview", json={"kind": "phonon", "structure_id": mg, "potential_id": nep,
                                                      "repeat": [1, 1, 1], "options": {}})
        check("フォノン: 単斜晶 (高対称点 M1 など) でも経路を組み立てられる", r.status_code == 200, r.text[:160])
        r = s.post(f"{BASE}/api/potentials/upload", files={"file": ("x.txt", b"hello world\n")}, data={"name": "bad"})
        check("NEP でないファイルは 400", r.status_code == 400, r.json().get("detail", "")[:40])
        r = s.post(f"{BASE}/api/potentials/upload", files={"file": ("nep.txt", b"nep4 1 Ni\ncutoff 6 5\n")}, data={"name": "Ni only"})
        check("NEP の形式ならアップロードできる", r.status_code == 200, r.text[:80])
        ni = r.json()["id"]

        # --- プレビューと入力の検証 ---
        r = s.post(f"{BASE}/api/md/preview", json=md_params(cu, nep))
        p = r.json()
        check("プレビュー", r.status_code == 200 and p["n_atoms"] == 256 and p["total_steps"] == 2000 and "ensemble npt_scr 300 600" in p["run_in"], str({k: p.get(k) for k in ("n_atoms", "total_steps", "estimated_seconds")}))
        check("プレビューは作業ファイルを残さない", not any((DATA / "tmp").iterdir()))
        r = s.post(f"{BASE}/api/md/preview", json=md_params(cu, ni))
        check("対応していない元素は 400", r.status_code == 400 and "Cu" in r.json()["detail"], r.json().get("detail", ""))
        r = s.post(f"{BASE}/api/md/preview", json=md_params(cu, nep, repeat=[30, 30, 30]))
        check("原子数が目安を超えても警告だけで通る", r.status_code == 200 and r.json()["n_atoms"] == 108000 and any("目安" in n for n in r.json()["notes"]), str(r.json().get("notes", r.text))[:70])
        big_view = s.get(f"{BASE}/api/structures/{cu}", params={"na": 30, "nb": 30, "nc": 30}).json()
        check("大きいセルの 3D 表示は間引く", big_view["repeat"]["over_guideline"] and not big_view["repeat"]["too_many"] and big_view["view"]["shown"] <= 6000 and big_view["view"]["n_atoms"] == 108000, str(big_view["view"]["shown"]))
        r = s.post(f"{BASE}/api/md/preview", json=md_params(cu, nep, repeat=[3, 3, 25]))
        check("細長すぎるセルは直し方つきで 400", r.status_code == 400 and "a を 5 以上" in r.json()["detail"] and "c を 16 以下" in r.json()["detail"], r.json().get("detail", ""))
        check("直し方どおりにすれば通る", s.post(f"{BASE}/api/md/preview", json=md_params(cu, nep, repeat=[5, 5, 25])).status_code == 200 and s.post(f"{BASE}/api/md/preview", json=md_params(cu, nep, repeat=[3, 3, 16])).status_code == 200)
        r = s.post(f"{BASE}/api/md/preview", json=md_params(cu, nep, stages=[{"ensemble": "nvt", "steps": 9_000_000, "temperature": 300}] * 2))
        check("合計ステップの上限超えは 400", r.status_code == 400, r.json().get("detail", ""))
        r = s.post(f"{BASE}/api/md/preview", json=md_params(cu, nep, stages=[{"ensemble": "nvt", "steps": 100}]))
        check("温度なしの NVT は 400", r.status_code == 400, r.json().get("detail", ""))
        r = s.post(f"{BASE}/api/md/preview", json=md_params(cu, nep, stages=[{"ensemble": "nvt; rm -rf /", "steps": 100, "temperature": 300}]))
        check("不正な ensemble は 422", r.status_code == 422)
        r = s.post(f"{BASE}/api/md/preview", json=md_params(cu, nep, minimize=True, stages=[{"ensemble": "nve", "steps": 100, "temperature": 300}]))
        check("NVE + 構造最適化のプレビュー", r.status_code == 200 and "minimize" in r.json()["run_in"] and "velocity 300" in r.json()["run_in"])

        # --- 実行 ---
        r = s.post(f"{BASE}/api/md/jobs", json=md_params(cu, nep, name="Cu test"))
        check("ジョブを投入", r.status_code == 200, r.text[:80])
        first = r.json()["id"]
        second = s.post(f"{BASE}/api/md/jobs", json=md_params(cu, nep, repeat=[6, 6, 6], stages=[{"ensemble": "nvt", "steps": 200000, "temperature": 300}])).json()["id"]
        third = s.post(f"{BASE}/api/md/jobs", json=md_params(cu, nep)).json()["id"]
        time.sleep(2.5)
        states = {j["id"]: j["status"] for j in s.get(f"{BASE}/api/jobs").json()}
        check("同時に動くのは 1 つだけ", list(states.values()).count("running") <= 1 and states[third] == "queued", str(states))
        job = wait_status(s, first, ("done", "failed"))
        check("1 つ目が完了", job["status"] == "done" and job["progress"] == 1.0, job["message"])
        names = {f["name"] for f in job["files"]}
        check("結果ファイルがそろっている", {"thermo.out", "dump.xyz", "run.in", "restart.xyz", "CONTCAR", "analysis/thermo.csv"} <= names, str(sorted(names)))
        check("グラフ画像も出力", any(n.endswith(".png") for n in names))
        thermo = s.get(f"{BASE}/api/jobs/{first}/thermo").json()
        temps = thermo["series"]["temperature"]
        check("熱力学量の時系列", thermo["n_rows"] == 40 and len(thermo["stages"]) == 2 and 100 < temps[10] < 400, f"rows={thermo['n_rows']} T[10]={temps[10]:.0f}")
        check("ステージごとの要約", len(thermo["summary"]) == 2 and thermo["summary"][0]["ensemble"] == "nvt_nhc", str(thermo["summary"][0].get("temperature_mean")))
        traj = s.get(f"{BASE}/api/jobs/{first}/trajectory").json()
        check("トラジェクトリ", traj["n_frames_total"] == 10 and len(traj["frames"]) == 10 and len(traj["frames"][0]["positions"]) == 256 and traj["symbols"][0] == "Cu", f"frames={traj['n_frames_total']}")
        r = s.get(f"{BASE}/api/jobs/{first}/file", params={"name": "thermo.out"})
        check("ファイルのダウンロード", r.status_code == 200 and "attachment" in r.headers["content-disposition"])
        for bad in ("../../app.db", "/etc/passwd", "metadata.json", "../1/run.in"):
            check(f"ディレクトリ外・非公開は取れない: {bad}", s.get(f"{BASE}/api/jobs/{first}/file", params={"name": bad}).status_code == 400)
        r = s.get(f"{BASE}/api/jobs/{first}/zip")
        zipped = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        check("zip のダウンロード", r.status_code == 200 and f"job{first}/thermo.out" in zipped, f"{len(zipped)} files")
        time.sleep(0.5)
        check("zip の一時ファイルは消える", not any((DATA / "tmp").iterdir()))
        r = s.post(f"{BASE}/api/jobs/{first}/save-final", json={})
        check("最終構造を構造として登録", r.status_code == 200 and s.get(f"{BASE}/api/structures/{r.json()['id']}").json()["n_atoms"] == 256)

        # --- 中止・取り消し・削除 ---
        job = wait_status(s, second, ("running",))
        time.sleep(2)
        job = s.get(f"{BASE}/api/jobs/{second}").json()
        check("実行中は進捗と残り時間が出る", 0 < job["progress"] < 1 and job["remaining_seconds"] is not None, f"{job['progress']:.2f} 残り{job['remaining_seconds']}s")
        check("実行中でも途中経過のグラフを読める", s.get(f"{BASE}/api/jobs/{second}/thermo").json()["n_rows"] > 0)
        check("実行中のジョブは削除できない", s.delete(f"{BASE}/api/jobs/{second}").status_code == 400)
        check("待ちジョブを取り消し", s.post(f"{BASE}/api/jobs/{third}/cancel").json()["was"] == "queued" and s.get(f"{BASE}/api/jobs/{third}").json()["status"] == "cancelled")
        pid = subprocess.run(["pgrep", "-x", "gpumd"], capture_output=True, text=True).stdout.split()
        s.post(f"{BASE}/api/jobs/{second}/cancel")
        job = wait_status(s, second, ("cancelled", "failed", "done"), timeout=30)
        time.sleep(0.5)
        still = subprocess.run(["pgrep", "-x", "gpumd"], capture_output=True, text=True).stdout.split()
        check("実行中のジョブを中止すると gpumd が止まる", job["status"] == "cancelled" and pid and not set(pid) & set(still), f"pid {pid} -> {still}")
        check("中止したジョブを削除", s.delete(f"{BASE}/api/jobs/{second}").status_code == 200 and not (DATA / "jobs" / str(second)).exists())
        # --- MD 以外の計算 ---
        listing = s.get(f"{BASE}/api/kinds").json()
        keys = [k["key"] for k in listing["kinds"]]
        # 11 種類 + GPUMD-Tutorials 由来の 10 種類 (tutorial_kinds.py)
        check("計算の種類は 24", keys == ["md", "static", "diffusion", "thermal", "free_energy", "tensile", "mc", "pimd",
                                       "shock", "ttm", "al", "phonon", "vdos", "expansion", "melting", "elastic_t",
                                       "fatigue", "impact", "deposition", "surface", "doas", "plumed", "transition", "finetune"], str(keys))
        overview = s.get(f"{BASE}/api/jobs/{first}/results").json()
        thermo_ds = next(d for d in overview["datasets"] if d["key"] == "thermo")
        column_keys = [c["key"] for c in thermo_ds["columns"]]
        check("MD の結果は熱力学量の表として軸を選べる", {"time_ps", "temperature", "volume", "pressure", "potential_energy_per_atom", "a"} <= set(column_keys), str(len(column_keys)))
        table = s.get(f"{BASE}/api/jobs/{first}/dataset", params={"key": "thermo"}).json()
        check("表のデータ (温度と体積など列ごと)", table["n_rows"] == 40 and len(table["data"]["volume"]) == 40 and len(table["data"]["temperature"]) == 40)
        check("存在しない表は 400", s.get(f"{BASE}/api/jobs/{first}/dataset", params={"key": "nothing"}).status_code == 400)
        check("表の名前に記号は使えない", s.get(f"{BASE}/api/jobs/{first}/dataset", params={"key": "../x"}).status_code == 422)

        def calc(kind, structure, repeat, options, **over):
            return {"kind": kind, "structure_id": structure, "potential_id": nep, "repeat": repeat, "time_step": 1.0,
                    "thermo_interval": 100, "traj_interval": 500, "options": options, **over}

        mgo = s.post(f"{BASE}/api/structures/sample", json={"key": "MgO"}).json()["id"]
        r = s.post(f"{BASE}/api/calc/preview", json=calc("static", cu, [2, 2, 2], {}))
        check("静的計算のプレビュー", r.status_code == 200 and "compute_elastic" in r.json()["run_in"], r.text[:80])
        check("種類が正しくなければ 400", s.post(f"{BASE}/api/calc/preview", json=calc("nope", cu, [2, 2, 2], {})).status_code == 400)
        r = s.post(f"{BASE}/api/calc/preview", json=calc("thermal", cu, [4, 4, 4], {"method": "x; rm -rf /"}))
        check("選択肢にない値は 400", r.status_code == 400, r.json().get("detail", ""))
        r = s.post(f"{BASE}/api/calc/preview", json=calc("thermal", cu, [4, 4, 4], {"temperature": -5}))
        check("範囲外の数値は 400", r.status_code == 400, r.json().get("detail", ""))
        r = s.post(f"{BASE}/api/calc/preview", json=calc("diffusion", mgo, [3, 3, 3], {"ic_element": "Li"}))
        check("構造にない元素のイオン伝導度は 400", r.status_code == 400, r.json().get("detail", ""))
        r = s.post(f"{BASE}/api/calc/preview", json=calc("mc", cu, [5, 5, 5], {}))
        check("元素 1 種類の Monte Carlo は 400", r.status_code == 400, r.json().get("detail", ""))
        r = s.post(f"{BASE}/api/calc/preview", json=calc("mc", mgo, [2, 2, 2], {}))
        check("セルが小さい Monte Carlo は 400", r.status_code == 400, r.json().get("detail", "")[:60])
        r = s.post(f"{BASE}/api/calc/preview", json=calc("al", cu, [3, 3, 3], {}))
        check("ポテンシャル 1 つの Active Learning は 400", r.status_code == 400, r.json().get("detail", ""))

        big_job = s.post(f"{BASE}/api/md/jobs", json=md_params(cu, nep, repeat=[30, 30, 30], traj_interval=100, stages=[{"ensemble": "nvt", "steps": 200, "temperature": 300}])).json()["id"]
        job = wait_status(s, big_job, ("done", "failed"), timeout=300)
        traj = s.get(f"{BASE}/api/jobs/{big_job}/trajectory").json()
        check("10 万原子の MD が実行できる", job["status"] == "done" and job["n_atoms"] == 108000, job["message"])
        check("大きいトラジェクトリは原子を間引いて表示", traj["n_atoms"] == 108000 and len(traj["symbols"]) <= 6000 and len(traj["frames"][0]["positions"]) == len(traj["symbols"]), f"{len(traj['symbols'])} atoms x {len(traj['frames'])} frames")
        static_job = s.post(f"{BASE}/api/calc/jobs", json=calc("static", cu, [2, 2, 2], {})).json()["id"]
        tensile_job = s.post(f"{BASE}/api/calc/jobs", json=calc("tensile", cu, [4, 4, 4], {"equil_steps": 500, "steps": 3000, "strain_rate": 2e10})).json()["id"]
        diffusion_job = s.post(f"{BASE}/api/calc/jobs", json=calc("diffusion", mgo, [3, 3, 3], {"temperature": 2000, "equil_steps": 500, "steps": 2000, "ic_element": "O", "ic_charge": -2, "correlation": 100})).json()["id"]
        job = wait_status(s, static_job, ("done", "failed"))
        res = s.get(f"{BASE}/api/jobs/{static_job}/results").json()
        labels = {h["label"]: h["value"] for h in res["highlights"]}
        check("静的計算が完了し、弾性率とエネルギーが出る", job["status"] == "done" and 100 < labels.get("体積弾性率 B", 0) < 200 and -4.4 < labels.get("エネルギー", 0) < -4.2, f"{job['message']} B={labels.get('体積弾性率 B')} E={labels.get('エネルギー')}")
        check("静的計算の表 (状態方程式・弾性定数)", {"cohesive", "elastic"} <= {d["key"] for d in res["datasets"]})
        eos = s.get(f"{BASE}/api/jobs/{static_job}/dataset", params={"key": "cohesive"}).json()
        check("状態方程式のデータ", eos["n_rows"] > 50 and len(eos["data"]["scale"]) == len(eos["data"]["energy_per_atom"]))
        job = wait_status(s, tensile_job, ("done", "failed"))
        res = s.get(f"{BASE}/api/jobs/{tensile_job}/results").json()
        labels = {h["label"]: h["value"] for h in res["highlights"]}
        check("引張試験が完了し、ヤング率と応力ひずみ曲線が出る", job["status"] == "done" and labels.get("ヤング率", 0) > 10 and any(d["key"] == "stress_strain" for d in res["datasets"]), f"{job['message']} E={labels.get('ヤング率')}")
        check("引張試験の進捗は 100%", job["progress"] == 1.0 and job["total_steps"] == 3500)
        job = wait_status(s, diffusion_job, ("done", "failed"))
        res = s.get(f"{BASE}/api/jobs/{diffusion_job}/results").json()
        labels = [h["label"] for h in res["highlights"]]
        check("拡散が完了し、元素別の拡散係数とイオン伝導度が出る", job["status"] == "done" and "拡散係数 D (Mg)" in labels and "イオン伝導度 (O)" in labels, f"{job['message']} {labels}")
        msd = s.get(f"{BASE}/api/jobs/{diffusion_job}/dataset", params={"key": "msd"}).json()
        check("MSD の表に元素別の列がある", {"time", "msd_Mg", "msd_O"} <= set(msd["data"]))
        check("結果の表は csv でも取れる", s.get(f"{BASE}/api/jobs/{diffusion_job}/file", params={"name": "analysis/msd.csv"}).status_code == 200)
        check("作り直し用に入力が保存されている", s.get(f"{BASE}/api/jobs/{diffusion_job}").json()["params"]["options"]["ic_element"] == "O")
        s.delete(f"{BASE}/api/structures/{mgo}")

        check("構造を削除", s.delete(f"{BASE}/api/structures/{cu}").status_code == 200)
        check("既定のポテンシャルは削除できない", s.delete(f"{BASE}/api/potentials/{nep}").status_code == 400)
        check("追加したポテンシャルは削除できる", s.delete(f"{BASE}/api/potentials/{ni}").status_code == 200)

        # --- 設定・ログアウト ---
        r = s.put(f"{BASE}/api/settings/mp-key", json={"key": "abc123"})
        check("MP キーを保存 (値は返さない)", r.json() == {"mp_key_set": True} and oct((DATA / "config.json").stat().st_mode)[-3:] == "600")
        check("記号入りのキーは 422", s.put(f"{BASE}/api/settings/mp-key", json={"key": "a b;c"}).status_code == 422)
        s.put(f"{BASE}/api/settings/mp-key", json={"key": ""})
        s.post(f"{BASE}/api/auth/logout")
        check("ログアウト後は見られない", s.get(f"{BASE}/api/jobs").status_code == 401)
        check("API ドキュメントは公開しない", requests.get(f"{BASE}/docs").status_code == 404 and requests.get(f"{BASE}/openapi.json").status_code == 404)
    finally:
        server.terminate()
        server.wait(timeout=10)
        subprocess.run(["pkill", "-f", f"{DATA}/jobs"], check=False)
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} / {len(checks)} OK")
    if failed:
        print("NG:", *failed, sep="\n  ")
        sys.exit(1)
    shutil.rmtree(DATA, ignore_errors=True)


if __name__ == "__main__":
    main()
