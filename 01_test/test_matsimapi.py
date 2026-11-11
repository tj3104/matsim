"""MatSimAPI の通しテスト。Python の Client・コマンド・MCP サーバーと、MatSimApp との同時利用を確かめる。

    ~/.venv/gpumd312/bin/python 01_test/test_matsimapi.py
"""
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_matsimapi"))
os.environ["MATSIM_DATA_DIR"] = str(DATA)
PORT = 8047
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
STAGES = [{"ensemble": "nvt", "steps": 300, "temperature": 300}]
checks = []

import requests  # noqa: E402
from ase.build import bulk  # noqa: E402
from matsimapi import Client, JobFailed, NotFound, runtime  # noqa: E402


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def raises(error, function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except error:
        return True
    return False


def stop_workers():
    subprocess.run(["pkill", "-f", f"matsimapi worker.*{DATA}"], check=False)
    for line in subprocess.run(["pgrep", "-f", "matsimapi worker"], capture_output=True, text=True).stdout.split():
        environ = Path(f"/proc/{line}/environ").read_bytes()
        if f"MATSIM_DATA_DIR={DATA}".encode() in environ:
            os.kill(int(line), signal.SIGTERM)


def test_client() -> tuple[Client, int]:
    client = Client()
    check("データ置き場が使われる", client.system()["data_dir"] == str(DATA.resolve()))
    check("ポテンシャルと計算の種類を読める", client.potentials() and client.kinds()["kinds"][0]["key"] == "md")
    cu = client.add_sample("Cu")
    from_atoms = client.add_structure(bulk("Si", "diamond", a=5.43, cubic=True), "Si")
    poscar = client.add_structure(ROOT / "01_test" / "POSCAR_Cu")
    check("構造を 3 通りで登録", len(client.structures()) == 3 and client.atoms(from_atoms).get_chemical_formula() == "Si8")
    check("存在しない構造は NotFound", raises(NotFound, client.structure, 9999))
    check("壊れた入力は ValueError", raises(ValueError, client.submit, "md", cu, stages=[]))
    check("MD に options は渡せない", raises(ValueError, client.preview, "md", cu, options={"a": 1}, stages=STAGES))

    preview = client.preview("md", cu, repeat=(3, 3, 3), stages=STAGES)
    check("プレビュー", preview["n_atoms"] == 108 and preview["run_in"] and preview["total_steps"] == 300)
    check("スーパーセルの既定値は厚み 12 Å 以上", client.structure(cu)["default_repeat"] == [4, 4, 4])
    check("repeat を省くと既定値を使う", client.preview("md", cu, stages=STAGES)["n_atoms"] == 256)
    check("明示した repeat が優先", client.preview("md", cu, repeat=(1, 1, 1), stages=STAGES)["n_atoms"] == 4)
    check("プレビューでは投入されない", client.jobs() == [])
    check("実行前はワーカーがいない", not runtime.is_running())

    job = client.submit("md", cu, repeat=(3, 3, 3), stages=STAGES, traj_interval=100)
    check("投入でワーカーが裏で起動", runtime.is_running() or time.sleep(1) or runtime.is_running())
    job.wait(120)
    check("MD が完了", job.status == "done", job.data["message"])
    check("既定のポテンシャルは NEP89", "NEP89" in job.data["summary"]["potential"])
    check("熱力学量", job.thermo()["n_rows"] > 0 and job.thermo()["summary"])
    results = job.results()
    check("表の一覧と 1 表", any(d["key"] == "thermo" for d in results["datasets"]) and job.dataset("thermo")["data"]["temperature"])
    check("run.in を読める", "potential" in job.read_text("run.in"))

    cna = job.analyze("cna").wait(120)
    check("解析のジョブ", cna.status == "done" and cna.result()["fractions"][0]["type"])
    again = job.analyze("cna")
    check("同じ条件の解析は使い回す", again.reused and again.id == cna.id)
    check("原子分布", job.distribution(axis="x", bin_width=2.0)["all_species"] == ["Cu"])
    saved = job.save_final_structure("最終")
    check("最終構造の登録", client.structure(saved)["n_atoms"] == 108)
    zipped = job.zip(DATA / "tmp" / "result.zip")
    check("zip", zipped.stat().st_size > 0)

    model = client.save_model("substitute", cu, {"from": "Cu", "to": "Ni", "amount": "count", "count": 1})
    check("モデリング", "Ni" in client.structure(model)["formula"])

    diffusion = client.preview("diffusion", poscar, repeat=(3, 3, 3))
    check("MD 以外のプレビュー", diffusion["n_atoms"] == 108 and "run_in" in diffusion)

    long_job = client.submit("md", cu, repeat=(4, 4, 4), stages=[{"ensemble": "nvt", "steps": 500000, "temperature": 300}])
    while long_job.refresh().status != "running":
        time.sleep(0.5)
    long_job.cancel()
    check("中止で JobFailed", raises(JobFailed, long_job.wait, 60))
    check("中止の状態", long_job.status == "cancelled")
    long_job.delete()
    check("削除", raises(NotFound, client.job, long_job.id))
    return client, cu


def test_cli() -> None:
    def run(*args):
        return subprocess.run([sys.executable, "-m", "matsimapi", *args], capture_output=True, text=True, timeout=120)
    info = json.loads(run("info").stdout)
    check("matsimapi info", info["data_dir"] == str(DATA.resolve()) and "limits" in info)
    check("matsimapi jobs", len(json.loads(run("jobs").stdout)) >= 2)
    check("matsimapi --data-dir", json.loads(run("--data-dir", str(DATA), "structures").stdout)[0]["n_atoms"] > 0)


async def test_mcp(cu: int) -> None:
    from mcp import Client as McpClient
    from mcp import StdioServerParameters

    def value(result):
        assert not result.is_error, result.content
        data = result.structured_content
        if data is None:
            return json.loads(result.content[0].text)
        return data["result"] if set(data) == {"result"} else data

    params = StdioServerParameters(command=sys.executable, args=["-m", "matsimapi", "mcp"],
                                   env={**os.environ, "MATSIM_DATA_DIR": str(DATA)}, cwd=str(DATA))
    async with McpClient(params, read_timeout_seconds=120) as mcp:
        tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
        check("MCP のツール", {"submit_calculation", "preview_calculation", "get_job_results"} <= set(tools), len(tools))
        check("投入前の確認の順番を説明する", "preview_calculation" in (mcp.instructions or ""))
        check("読むだけのツールの印", tools["list_jobs"].annotations.read_only_hint)
        check("消すツールの印", tools["delete_jobs"].annotations.destructive_hint)
        check("計算の種類", any(k["key"] == "diffusion" for k in value(await mcp.call_tool("list_calculation_kinds"))))
        kind = value(await mcp.call_tool("describe_calculation_kind", {"kind": "diffusion"}))
        check("入力欄の定義", kind["fields"])
        args = {"kind": "md", "structure_id": cu, "repeat": [3, 3, 3], "stages": STAGES}
        preview = value(await mcp.call_tool("preview_calculation", args))
        check("MCP のプレビュー", preview["n_atoms"] == 108 and preview["estimated_seconds"] > 0)
        made = value(await mcp.call_tool("submit_calculation", args))
        job_id = made["jobs"][0]["id"]
        done = value(await mcp.call_tool("wait_for_job", {"job_id": job_id, "timeout_seconds": 120}))
        check("MCP で MD が完了", done["status"] == "done", done["message"])
        overview = value(await mcp.call_tool("get_job_results", {"job_id": job_id}))
        check("MCP の結果", any(d["key"] == "thermo" for d in overview["datasets"]))
        table = value(await mcp.call_tool("get_job_dataset", {"job_id": job_id, "key": "thermo", "columns": ["temperature"], "max_rows": 2}))
        check("列と行を絞れる", list(table["data"]) == ["temperature"] and len(table["data"]["temperature"]) <= 2)
        analysis = value(await mcp.call_tool("submit_analysis", {"kind": "cna", "job_id": job_id}))
        finished = value(await mcp.call_tool("wait_for_job", {"job_id": analysis["id"], "timeout_seconds": 60}))
        result = value(await mcp.call_tool("get_analysis_result", {"job_id": analysis["id"]}))
        check("MCP の解析で重いデータを落とす", finished["status"] == "done" and "view" not in result and result["fractions"])
        bad = await mcp.call_tool("get_job", {"job_id": 99999})
        check("誤りは説明つきで返る", bad.is_error and "見つかりません" in bad.content[0].text)
        text = value(await mcp.call_tool("read_job_file", {"job_id": job_id, "name": "run.in"}))
        check("入力ファイルを読める", "potential" in text["text"])
        escape = await mcp.call_tool("read_job_file", {"job_id": job_id, "name": "../../app.db"})
        check("ジョブの外のファイルは読めない", escape.is_error)


def test_with_app(client: Client, cu: int) -> None:
    """同じデータ置き場で画面のサーバーと Python が一緒に動く。ワーカーは 1 つで、引き継げる。"""
    while runtime.is_running():
        stop_workers()
        time.sleep(0.5)
    idle = subprocess.Popen([sys.executable, "-m", "matsimapi", "worker"], env=os.environ)
    while not runtime.is_running():
        time.sleep(0.2)
    log = open(DATA / "server.log", "w")
    server = subprocess.Popen([sys.executable, "-m", "matsimapp", "--port", str(PORT)], env=os.environ, stdout=log,
                              stderr=subprocess.STDOUT, cwd=DATA)
    try:
        session = requests.Session()
        for _ in range(100):
            try:
                session.get(f"{BASE}/api/status", timeout=2)
                break
            except requests.RequestException:
                time.sleep(0.3)
        account = {"username": "tester", "password": "correct-horse-battery"}
        check("画面のアカウント作成", session.post(f"{BASE}/api/auth/setup", json=account, headers=H).ok)
        job = client.submit("md", cu, repeat=(3, 3, 3), stages=STAGES, name="Python から")
        listed = session.get(f"{BASE}/api/jobs", headers=H).json()
        check("Python のジョブが画面に出る", any(j["id"] == job.id for j in listed))
        job.wait(120)
        check("常駐ワーカーが実行", job.status == "done")
        body = {"structure_id": cu, "potential_id": client.potentials()[0]["id"], "repeat": [3, 3, 3], "stages": STAGES}
        idle.send_signal(signal.SIGTERM)
        idle.wait(20)
        web_id = session.post(f"{BASE}/api/md/jobs", json=body, headers=H).json()["id"]
        client.start_worker = False
        web_job = client.job(web_id).wait(120)
        check("ワーカーが止まると画面のサーバーが引き継ぐ", web_job.status == "done", web_job.data["message"])
        queued = [client.submit("md", cu, repeat=(4, 4, 4), stages=[{"ensemble": "nvt", "steps": 2000, "temperature": 300}]) for _ in range(3)]
        overlap = 0
        while not all(j.refresh().finished for j in queued):
            overlap = max(overlap, sum(j.status == "running" for j in queued))
            time.sleep(0.2)
        check("GPU のジョブは 1 つずつ", overlap == 1 and all(j.status == "done" for j in queued), overlap)
    finally:
        server.terminate()
        server.wait(20)
        log.close()


def main() -> None:
    if os.environ.get("MATSIM_TEST_DATA") is None:
        shutil.rmtree(DATA, ignore_errors=True)
    try:
        client, cu = test_client()
        test_cli()
        asyncio.run(test_mcp(cu))
        test_with_app(client, cu)
    finally:
        stop_workers()
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 件 OK", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
