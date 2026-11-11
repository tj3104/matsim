"""構造相転移の通しテスト。テスト用のサーバーを立てて、
  - 構造相転移 (温度スキャン): Ti の hcp を昇温 → 降温 (実験では約 1155 K で hcp → bcc)
  - 構造相転移 (圧力スキャン): Fe の bcc を加圧 → 減圧 (実験では約 13 GPa で bcc → hcp)
  - 結晶構造の判定 (CNA の解析ジョブ): 構造 (fcc・bcc・hcp) と、上の Ti のジョブの時間変化
  - PLUMED の構造相転移向けの CV: bcc との類似度 × hcp との類似度・セルの比 c/a
を NEP89 で流し、結果と画面を確かめる。転移が起こるかどうかはポテンシャルしだいなので、起こったかは記録だけする。

    CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_transition.py
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
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_transition"))
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8036
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def upload(s, atoms, name):
    buf = io.StringIO()
    write(buf, atoms, format="vasp", direct=True)
    return s.post(f"{BASE}/api/structures/upload", files={"file": ("POSCAR", buf.getvalue())},
                  data={"name": name}).json()["id"]


def wait(s, ids, timeout):
    """ジョブが終わるまで待つ。間隔は 3 秒 (uvicorn が空いた接続を閉じる 5 秒と重ならないように)。"""
    start = time.time()
    states = {}
    while time.time() - start < timeout:
        try:
            states = {i: s.get(f"{BASE}/api/jobs/{i}").json()["status"] for i in ids}
        except requests.ConnectionError:   # 閉じかけの接続に当たったときは、次の回に問い合わせ直す
            time.sleep(1)
            continue
        if all(v not in ("queued", "running") for v in states.values()):
            return states, time.time() - start
        time.sleep(3)
    return states, time.time() - start


def stop_leftover_gpumd(data: Path) -> None:
    """data の下で動いている gpumd を止める (サーバーを止めても、別セッションで起動した計算は残るため)。
    gpumd は SIGTERM ですぐ止まらないことがあるので、10 秒待って残っていれば SIGKILL。"""
    import signal

    def leftovers():
        found = []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if Path(proc / "cmdline").read_bytes().split(b"\0")[0].endswith(b"gpumd") and \
                        str(Path(proc / "cwd").resolve()).startswith(str(data.resolve())):
                    found.append(int(proc.name))
            except OSError:
                continue
        return found
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = leftovers()
        for pid in pids:
            try:
                os.kill(pid, sig)
                print(f"  残っていた計算を止めました (pid {pid}, {sig.name})")
            except OSError:
                pass
        if not pids:
            return
        time.sleep(10)


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    # MATSIM_SERVER_WRAPPER: サーバーを包むスクリプト (合図の送り主を記録するときなど)。引数は ポート と ログ
    wrapper = os.environ.get("MATSIM_SERVER_WRAPPER")
    command = ([SERVER_PYTHON, wrapper, str(PORT), str(ROOT / "01_test" / "test_transition_signals.log")] if wrapper
               else [SERVER_PYTHON, "-m", "uvicorn", "matsimapp.main:app", "--port", str(PORT)])
    server = subprocess.Popen(command, cwd=ROOT,
                              env={**os.environ, "MATSIM_DATA_DIR": str(DATA)},
                              stdout=open(ROOT / "01_test" / "test_transition_server.log", "w"), stderr=subprocess.STDOUT,
                              start_new_session=True)   # テストに向けた合図 (Ctrl+C など) がサーバーに届かないように
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
        potential = s.get(f"{BASE}/api/potentials").json()[0]["id"]
        cu = s.post(f"{BASE}/api/structures/sample", json={"key": "Cu"}).json()["id"]
        ti = upload(s, bulk("Ti", "hcp", a=2.95, c=4.68, orthorhombic=True), "Ti hcp")
        fe = upload(s, bulk("Fe", "bcc", a=2.83, cubic=True), "Fe bcc")
        keys = [k["key"] for k in s.get(f"{BASE}/api/kinds").json()["kinds"]]
        check("種類がある: transition", "transition" in keys)

        # ---- 結晶構造の判定 (構造) ------------------------------------------------------------
        for sid, want in ((cu, "fcc"), (ti, "hcp"), (fe, "bcc")):
            task = s.post(f"{BASE}/api/analysis/cna", json={"structure_id": sid}).json()["id"]
            wait(s, [task], 60)
            res = s.get(f"{BASE}/api/jobs/{task}/cna").json()["result"]
            top = max(res["fractions"], key=lambda f: f["fraction"])
            check(f"結晶構造の判定: {want}", top["type"] == want and top["fraction"] == 1, res["fractions"])

        def submit(kind, structure, options, repeat):
            body = {"kind": kind, "structure_id": structure, "potential_id": potential, "repeat": list(repeat),
                    "time_step": 2.0, "thermo_interval": 200, "traj_interval": 0, "options": options}
            r = s.post(f"{BASE}/api/calc/preview", json=body)
            if not r.ok:
                check(f"プレビュー {kind}", False, r.text[:300])
                return None
            return s.post(f"{BASE}/api/calc/jobs", json=body).json()["id"]

        r = s.post(f"{BASE}/api/calc/preview", json={"kind": "transition", "structure_id": ti, "potential_id": potential,
                   "repeat": [6, 4, 4], "options": {"t_start": 500, "t_end": 500}})
        check("始めと終わりが同じならエラー", r.status_code == 400, r.text[:100])
        jobs = {
            "ti_heat": submit("transition", ti, {"t_start": 300, "t_end": 1800, "n_points": 16, "equil_steps": 10000,
                                                 "steps": 20000}, (6, 4, 4)),
            "fe_press": submit("transition", fe, {"control": "pressure", "p_start": 0, "p_end": 40, "n_points": 11,
                                                  "temperature": 300, "equil_steps": 10000, "steps": 20000}, (6, 6, 6)),
            "pl_sim": submit("plumed", fe, {"method": "watch", "cv1": "similarity", "target1": "BCC", "cv2": "similarity",
                                            "target2": "HCP", "temperature": 300, "pressure": 0, "equil_steps": 2000,
                                            "steps": 6000}, (4, 4, 4)),
            "pl_ca": submit("plumed", fe, {"cv1": "ca", "temperature": 300, "equil_steps": 2000, "steps": 10000,
                                           "pace": 200, "height": 0.2}, (4, 4, 4)),
        }
        states, took = wait(s, list(jobs.values()), 5400)
        print(f"  ({took:.0f} 秒)")
        res = {}
        for key, i in jobs.items():
            check(f"完了: {key}", states[i] == "done", s.get(f"{BASE}/api/jobs/{i}").json().get("message"))
            res[key] = s.get(f"{BASE}/api/jobs/{i}/results").json()
            bad = [n for n in res[key]["notes"] if "読めませんでした" in n]
            check(f"取り出しの失敗なし: {key}", not bad, bad)
        for key in ("ti_heat", "fe_press"):
            h = {x["label"]: x["value"] for x in res[key]["highlights"]}
            ds = [d["key"] for d in res[key]["datasets"]]
            check(f"結果の表とグラフ: {key}", {"volume", "structure_up", "structure_down", "cell", "angles"} <= set(ds), ds)
            print(f"  {key}:", h)
        pl = s.get(f"{BASE}/api/jobs/{jobs['pl_sim']}/dataset?key=colvar").json()["data"]
        check("PLUMED: bcc との類似度が高い (bcc の Fe)", pl["cv1_mean"][0] > 0.7 and pl["cv2_mean"][0] < 0.5,
              (pl["cv1_mean"][0], pl["cv2_mean"][0]))
        run_in = (DATA / "jobs" / str(jobs["pl_ca"]) / "run.in").read_text()
        check("PLUMED: セルの比はセルの形も変える NPT", "npt_ber" in run_in and " 0 0 0 0 0 0 " in run_in, run_in[-200:])
        ca = s.get(f"{BASE}/api/jobs/{jobs['pl_ca']}/dataset?key=colvar").json()["data"]["cv1"]
        check("PLUMED: c/a が動く (メタダイナミクスでセルが変形する)", max(ca) - min(ca) > 0.02, (min(ca), max(ca)))

        # ---- 結晶構造の判定 (ジョブの時間変化) ---------------------------------------------------
        task = s.post(f"{BASE}/api/analysis/cna", json={"job_id": jobs["ti_heat"], "frames": 40}).json()["id"]
        wait(s, [task], 300)
        cna = s.get(f"{BASE}/api/jobs/{task}/cna").json()
        evo = (cna["result"] or {}).get("evolution", {})
        check("結晶構造の判定: ジョブの時間変化", len(evo.get("time_ps", [])) >= 20, list(evo.get("fractions", {})))

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
            page.goto(f"{BASE}/#/jobs/{jobs['ti_heat']}")
            expect(page.get_by_role("heading", name="体積", exact=True)).to_be_visible(timeout=15000)
            page.wait_for_timeout(2500)
            page.screenshot(path=SHOTS / "t01_transition_ti.png", full_page=True)
            page.goto(f"{BASE}/#/jobs/{jobs['fe_press']}")
            expect(page.get_by_role("heading", name="体積", exact=True)).to_be_visible(timeout=15000)
            page.wait_for_timeout(2500)
            page.screenshot(path=SHOTS / "t02_transition_fe.png", full_page=True)
            page.get_by_role("link", name="結晶構造の判定").click()
            page.get_by_role("button", name="解析を始める").click()
            expect(page.get_by_role("heading", name="結晶構造の比率")).to_be_visible(timeout=120000)
            page.wait_for_timeout(2500)
            page.screenshot(path=SHOTS / "t03_cna_job.png", full_page=True)
            check("画面: 色を付けた原子の数が出る", "原子に色を付けています" in str(page.locator("p.note").all_inner_texts()))
            page.goto(f"{BASE}/#/calc/transition")
            expect(page.get_by_text("変えるもの").first).to_be_visible(timeout=15000)
            page.screenshot(path=SHOTS / "t04_transition_form.png", full_page=True)
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
        stop_leftover_gpumd(DATA)
        if os.environ.get("KEEP_DATA") != "1":
            shutil.rmtree(DATA, ignore_errors=True)
    check("ブラウザのエラーなし", not errors, errors)
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} OK" + (f"  失敗: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
