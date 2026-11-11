"""ジョブを動かす係が画面から独立していることの試験。

    ~/.venv/gpumd312/bin/python 01_test/test_worker_independent.py

一時フォルダのデータ置き場と、本物の画面のサーバー・ワーカーで確かめる。Cu の小さい MD を GPU で流す。
本番のデータ・本番のサーバー・systemd のサービスには触らない。
"""
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

DATA = Path(tempfile.mkdtemp(prefix="matsim_worker_"))
os.environ["MATSIM_DATA_DIR"] = str(DATA)
PORT = 8049
CASES = []

from matsimapi import Client  # noqa: E402

STAGES = [{"ensemble": "nvt", "steps": 20000, "temperature": 300}]


def check(name, condition, detail=""):
    CASES.append(bool(condition))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def wait(condition, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        value = condition()
        if value:
            return value
        time.sleep(0.2)
    return None


def workers() -> list[int]:
    found = []
    for pid in subprocess.run(["pgrep", "-f", "matsimapi worker"], capture_output=True, text=True).stdout.split():
        try:
            if f"MATSIM_DATA_DIR={DATA}".encode() in Path(f"/proc/{pid}/environ").read_bytes():
                found.append(int(pid))
        except OSError:
            pass
    return found


def lock_held() -> bool:
    fd = os.open(DATA / "worker.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def server() -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-m", "matsimapp", "--port", str(PORT)], env=os.environ,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def up():
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=1).status == 200
        except OSError:
            return False
    assert wait(up, 40), "画面のサーバーが起動しません"
    return proc


def stop(proc, sig=signal.SIGTERM):
    proc.send_signal(sig)
    proc.wait(timeout=20)


def holds_lock(pid: int) -> bool:
    return any("worker.lock" in os.readlink(f"/proc/{pid}/fd/{fd}") for fd in os.listdir(f"/proc/{pid}/fd"))


def main():
    client = Client(start_worker=False)
    cu = client.add_sample("Cu")
    # 1. 画面が起動すると、自分ではなく別のプロセスがワーカーになる
    app = server()
    check("画面がワーカーを起動する", wait(workers, 30), workers())
    check("ロックを持つのはワーカーで画面ではない", wait(lock_held, 20) and not holds_lock(app.pid))
    first = workers()[0]

    # 2. 計算中に画面を止めても、計算も順番待ちも進む
    made = [client.submit("md", cu, repeat=(4, 4, 4), stages=STAGES) for _ in range(2)]
    check("1 件目が動き出す", wait(lambda: made[0].refresh().status == "running", 30))
    stop(app)
    check("画面を止めてもワーカーは残る", first in workers())
    check("順番待ちまで全部終わる", wait(lambda: all(job.refresh().status == "done" for job in made), 180),
          [job.refresh().status for job in made])

    # 3. 計算中にワーカーが落ちても、画面が起動し直し、動いていた gpumd を見つけ直す
    app = server()
    job = client.submit("md", cu, repeat=(4, 4, 4), stages=[{**STAGES[0], "steps": 40000}])
    check("動き出す", wait(lambda: job.refresh().status == "running", 30))
    gpumd = job.data.get("pid") or None
    os.kill(first, signal.SIGKILL)
    check("新しいワーカーが起動する", wait(lambda: [w for w in workers() if w != first], 40), workers())
    check("計算は完了として記録される", wait(lambda: job.refresh().status == "done", 180), job.refresh().status)
    stop(app)

    # 4. 同時に投入された解析もワーカーが処理する
    check("解析もワーカーが処理する", job.analyze("cna").wait(60).status == "done")
    worker = workers()
    for pid in worker:
        os.kill(pid, signal.SIGTERM)
    check("片付け", wait(lambda: not workers(), 20))
    return gpumd


try:
    main()
finally:
    for pid in workers():
        os.kill(pid, signal.SIGKILL)
    shutil.rmtree(DATA, ignore_errors=True)
print("すべて成功" if all(CASES) else "失敗あり")
sys.exit(0 if all(CASES) else 1)
