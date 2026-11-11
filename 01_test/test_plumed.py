"""PLUMED (自由エネルギー面) の通しテスト。テスト用のサーバーを立てて、小さな Cu で
メタダイナミクス (1 次元: 配位数・Q6 / 2 次元: 配位数 × 体積) と CV の記録 (バイアスなし) を
PLUMED 対応の gpumd で流し、plumed.dat・COLVAR・HILLS・自由エネルギー面が取り出せるか、画面に出るかを確かめる。

    CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_plumed.py
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
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_plumed"))
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8033
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


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
    server = subprocess.Popen([SERVER_PYTHON, "-m", "uvicorn", "matsimapp.main:app", "--port", str(PORT)], cwd=ROOT,
                              env={**os.environ, "MATSIM_DATA_DIR": str(DATA)}, stdout=open(ROOT / "01_test" / "test_plumed_server.log", "w"),
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
        status = s.get(f"{BASE}/api/system").json() if s.get(f"{BASE}/api/system").ok else {}
        check("計算環境に PLUMED が出る", status.get("plumed", {}).get("available"), status.get("plumed"))
        check("種類がある: plumed", any(k["key"] == "plumed" for k in s.get(f"{BASE}/api/kinds").json()["kinds"]))
        potential = s.get(f"{BASE}/api/potentials").json()[0]["id"]
        cu = s.post(f"{BASE}/api/structures/sample", json={"key": "Cu"}).json()["id"]

        def body(options, repeat=(3, 3, 3)):
            return {"kind": "plumed", "structure_id": cu, "potential_id": potential, "repeat": list(repeat),
                    "time_step": 2.0, "thermo_interval": 100, "traj_interval": 0, "options": options}

        one = {"cv1": "cn", "temperature": 1300, "equil_steps": 1000, "steps": 20000, "pace": 100, "height": 0.1}
        preview = s.post(f"{BASE}/api/calc/preview", json=body(one)).json()
        run_in = preview.get("run_in", "")
        check("プレビューに run.in と plumed.dat", "plumed plumed.dat 1 0" in run_in and "METAD ARG=cv1.mean" in run_in
              and "COORDINATIONNUMBER" in run_in, run_in[-300:])
        r = s.post(f"{BASE}/api/calc/preview", json=body({**one, "cv1": "volume", "ensemble": "nvt"}))
        check("体積の CV は NVT ではエラー", r.status_code == 400 and "NPT" in r.json()["detail"], r.text[:120])
        r = s.post(f"{BASE}/api/calc/preview", json=body({**one, "species_a": "Zr"}))
        check("ない元素はエラー", r.status_code == 400 and "Zr" in r.json()["detail"], r.text[:120])
        r = s.post(f"{BASE}/api/calc/preview", json=body({**one, "steps": 500}))
        check("本計算が短すぎるとエラー", r.status_code == 400, r.text[:120])

        jobs = {
            "cn1d": s.post(f"{BASE}/api/calc/jobs", json=body(one)).json()["id"],
            "q61d": s.post(f"{BASE}/api/calc/jobs", json=body({"cv1": "q6", "temperature": 1300, "equil_steps": 500,
                                                                "steps": 3000, "pace": 100})).json()["id"],
            "cn2d": s.post(f"{BASE}/api/calc/jobs", json=body({"cv1": "cn", "cv2": "volume", "temperature": 1300,
                                                                "equil_steps": 1000, "steps": 10000, "pace": 100,
                                                                "height": 0.1})).json()["id"],
            "watch": s.post(f"{BASE}/api/calc/jobs", json=body({"method": "watch", "cv1": "q6", "cv2": "distance",
                                                                 "ensemble": "nvt", "temperature": 800,
                                                                 "equil_steps": 500, "steps": 5000,
                                                                 "print_interval": 20})).json()["id"],
        }
        start = time.time()
        while time.time() - start < 1200:
            states = {k: s.get(f"{BASE}/api/jobs/{i}").json()["status"] for k, i in jobs.items()}
            if all(v not in ("queued", "running") for v in states.values()):
                break
            time.sleep(3)
        print(f"  ({time.time() - start:.0f} 秒)", states)
        res = {}
        for key, i in jobs.items():
            check(f"完了: {key}", states[key] == "done", s.get(f"{BASE}/api/jobs/{i}").json().get("message"))
            res[key] = s.get(f"{BASE}/api/jobs/{i}/results").json()
            data_dir = DATA / "jobs" / str(i)
            check(f"PLUMED の出力: {key}", (data_dir / "COLVAR").is_file() and (data_dir / "plumed.dat.out").is_file()
                  and ((data_dir / "HILLS").is_file() or key == "watch"))
        h = {x["label"]: x["value"] for x in res["cn1d"]["highlights"]}
        keys = [d["key"] for d in res["cn1d"]["datasets"]]
        check("1 次元: 自由エネルギー曲線と収束の確認", "fes" in keys and "colvar" in keys
              and len(next(d for d in res["cn1d"]["datasets"] if d["key"] == "fes")["y"]) == 4, keys)
        check("1 次元: ガウス関数の数", h.get("置いたガウス関数の数") == 200, h)
        fes = s.get(f"{BASE}/api/jobs/{jobs['cn1d']}/dataset?key=fes").json()["data"]
        check("自由エネルギーの最小は 0", abs(min(fes["F"])) < 1e-9 and max(fes["F"]) > 0, max(fes["F"]))
        check("Q6 の 1 次元も動く", any(d["key"] == "fes" for d in res["q61d"]["datasets"]),
              [x["label"] for x in res["q61d"]["highlights"]])
        keys2 = {d["key"]: d for d in res["cn2d"]["datasets"]}
        check("2 次元: 自由エネルギー面 (地図) と各 CV の曲線", "fes2d" in keys2 and keys2["fes2d"]["style"] == "colormap"
              and keys2["fes2d"]["equal_axes"] is False and "fes_cv1_mean" in keys2 and "fes_cv2" in keys2, list(keys2))
        keysw = [d["key"] for d in res["watch"]["datasets"]]
        check("記録だけ: COLVAR と分布からの自由エネルギー面", "colvar" in keysw and "fes2d" in keysw, keysw)
        for key in jobs:
            bad = [n for n in res[key]["notes"] if "読めませんでした" in n]
            check(f"取り出しの失敗なし: {key}", not bad, bad)

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
            check("画面: 計算環境に PLUMED", page.get_by_text("PLUMED (自由エネルギー面)").is_visible())
            page.goto(f"{BASE}/#/calc/plumed")
            expect(page.get_by_text("CV 1", exact=True).first).to_be_visible(timeout=15000)
            page.screenshot(path=SHOTS / "pl01_form.png", full_page=True)
            for key, name in (("cn1d", "pl02_fes1d"), ("cn2d", "pl03_fes2d"), ("watch", "pl04_watch")):
                page.goto(f"{BASE}/#/jobs/{jobs[key]}")
                expect(page.get_by_role("heading", name="CV の時間変化")).to_be_visible(timeout=15000)
                page.wait_for_timeout(2500)
                page.screenshot(path=SHOTS / f"{name}.png", full_page=True)
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
