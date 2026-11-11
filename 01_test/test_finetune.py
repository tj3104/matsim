"""NEP のファインチューニング (SevenNet を先生にした蒸留 + 能動学習) の通しテスト。テスト用のサーバーを立てて、
256 原子の Cu で NEP89 を 7net-0 に合わせて学び直し、誤差が下がること・できたポテンシャルが一覧に登録されて
MD に使えること・さらに学び直す出発点にできること・使えない条件は止まることを確かめる。

    CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_finetune.py
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
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_finetune"))
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8043
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def wait(s, job, timeout=1800):
    start = time.time()
    while time.time() - start < timeout:
        try:
            status = s.get(f"{BASE}/api/jobs/{job}").json()
        except requests.ConnectionError:
            time.sleep(1)
            continue
        if status["status"] not in ("queued", "running"):
            return status
        time.sleep(5)
    raise TimeoutError("ジョブが終わりません")


def stop_leftovers(data: Path) -> None:
    """data の下で動いている gpumd・nep・ft_runner を止める (別セッションで起動した計算は残るため)。"""
    import signal
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = Path(proc / "cmdline").read_bytes().split(b"\0")
            if str(Path(proc / "cwd").resolve()).startswith(str(data.resolve())) and (
                    args[0].endswith((b"gpumd", b"nep")) or any(a.endswith(b"ft_runner.py") for a in args[1:3])):
                os.kill(int(proc.name), signal.SIGKILL)
        except OSError:
            continue


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    server = subprocess.Popen([SERVER_PYTHON, "-m", "uvicorn", "matsimapp.main:app", "--port", str(PORT)], cwd=ROOT,
                              env={**os.environ, "MATSIM_DATA_DIR": str(DATA)},
                              stdout=open(ROOT / "01_test" / "test_finetune_server.log", "w"), stderr=subprocess.STDOUT,
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
        nep89 = pots[0]["id"]
        seven = next(p["id"] for p in pots if p.get("engine") == "sevennet")
        cu = s.post(f"{BASE}/api/structures/sample", json={"key": "Cu"}).json()["id"]
        kinds = {k["key"]: k for k in s.get(f"{BASE}/api/kinds").json()["kinds"]}
        check("計算の一覧にファインチューニングがある", "finetune" in kinds, kinds.get("finetune", {}).get("label"))

        options = {"teacher": "sevennet:7net-0", "temperatures": "300,900", "ensemble": "npt", "pressure": 0,
                   "md_steps": 4000, "sample_interval": 200, "threshold": 0.04, "max_iterations": 2, "max_add": 20,
                   "generations": 1000, "population": 50, "n_rattle": 4, "init_max": 30, "init_min": 15, "gamma_select": 2.0}
        job = {"kind": "finetune", "name": "Cu FT", "structure_id": cu, "potential_id": nep89, "repeat": [4, 4, 4],
               "options": options}
        r = s.post(f"{BASE}/api/calc/preview", json={**job, "potential_id": seven})
        check("SevenNet を出発点にすると止まる", r.status_code == 400 and "出発点" in r.json()["detail"], r.json().get("detail"))
        r = s.post(f"{BASE}/api/calc/preview", json={**job, "options": {**options, "init_min": 40}})
        check("下限が上限より大きいと止まる", r.status_code == 400 and "下限" in r.json()["detail"], r.json().get("detail"))
        r = s.post(f"{BASE}/api/calc/preview", json={**job, "repeat": [2, 2, 2]})
        check("セルが薄いと止まる", r.status_code == 400 and "12 Å" in r.json()["detail"], r.json().get("detail"))
        preview = s.post(f"{BASE}/api/calc/preview", json=job).json()
        check("確認画面: ft_job.json と所要時間の目安", "fine_tune" not in preview["run_in"] and "ft_job.json" in preview["run_in"]
              and preview["n_atoms"] == 256 and 60 < preview["estimated_seconds"] < 3600,
              (preview.get("n_atoms"), preview.get("estimated_seconds")))

        job_id = s.post(f"{BASE}/api/calc/jobs", json=job).json()["id"]
        time.sleep(20)
        running = s.get(f"{BASE}/api/jobs/{job_id}").json()
        check("実行中: いまの工程が出る", running["status"] == "running" and running.get("phase"), running.get("phase"))
        status = wait(s, job_id)
        check("ファインチューニング: 完了", status["status"] == "done", status.get("message"))
        res = s.get(f"{BASE}/api/jobs/{job_id}/results").json()
        h = {x["label"]: x["value"] for x in res["highlights"]}
        check("誤差が下がる (元 → 最後、95%)", h.get("力の誤差 (最後、95%)", 9) < h.get("力の誤差 (元のポテンシャル、95%)", 0) * 0.7,
              (h.get("力の誤差 (元のポテンシャル、95%)"), h.get("力の誤差 (最後、95%)")))
        check("結果: 学習曲線・外挿度・記録・状態方程式・力の比較", {"learning", "gamma", "iterations", "eos", "parity"}
              <= {d["key"] for d in res["datasets"]}, [d["key"] for d in res["datasets"]])
        log = (DATA / "jobs" / str(job_id) / "gpumd.out").read_text()
        check("0 段目でダブりを除き、下限 15 まで足して学び、1 段目から外挿度を見る",
              "似ていない" in log and "下限 15 まで" in log and "外挿度 γ: 最大" in log,
              [x for x in log.splitlines() if "似ていない" in x or "外挿度 γ" in x][:3])
        name = h.get("登録したポテンシャル")
        pots = s.get(f"{BASE}/api/potentials").json()
        new = next((p for p in pots if p["name"] == name), None)
        check("できたポテンシャルが一覧に登録される (Cu のみ)", new is not None and new["elements"] == ["Cu"]
              and new["available"], name)
        restart = DATA / "potentials" / f"{new['id']}.restart"
        check("restart も保存される (さらに学び直せる)", restart.is_file())

        md = {"name": "Cu FT MD", "structure_id": cu, "potential_id": new["id"], "repeat": [4, 4, 4], "time_step": 1.0,
              "minimize": False, "thermo_interval": 100, "traj_interval": 0,
              "stages": [{"ensemble": "npt", "steps": 2000, "temperature": 300, "pressure": 0}]}
        md_id = s.post(f"{BASE}/api/md/jobs", json=md).json()["id"]
        status = wait(s, md_id)
        check("できたポテンシャルで MD (GPUMD) ができる", status["status"] == "done", status.get("message"))
        r = s.post(f"{BASE}/api/calc/preview", json={**job, "potential_id": new["id"]})
        check("できたポテンシャルを出発点にできる", r.status_code == 200, r.text[:200])

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
            page.goto(f"{BASE}/#/calc/finetune")
            expect(page.get_by_text("先生のモデル (SevenNet)").first).to_be_visible(timeout=15000)
            page.screenshot(path=SHOTS / "f01_finetune_form.png", full_page=True)
            page.goto(f"{BASE}/#/jobs/{job_id}")
            expect(page.get_by_text("段ごとの力の誤差").first).to_be_visible(timeout=20000)
            page.wait_for_timeout(2000)
            page.screenshot(path=SHOTS / "f02_finetune_result.png", full_page=True)
            browser.close()

        s.delete(f"{BASE}/api/potentials/{new['id']}")
        check("ポテンシャルを消すと restart も消える", not restart.is_file())
    finally:
        server.terminate()
        server.wait(timeout=10)
        stop_leftovers(DATA)
        if os.environ.get("KEEP_DATA") != "1":
            shutil.rmtree(DATA, ignore_errors=True)
    check("ブラウザのエラーなし", not errors, errors)
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} OK" + (f"  失敗: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
