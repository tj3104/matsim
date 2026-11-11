"""GPUMD がない環境の通しテスト。GPUMD の場所を存在しない所にして (MATSIM_MD_ENGINE=auto)、NEP89 の
MD・静的計算・フォノン分散が CPU (calorine の CPUNEP) で動き、GPUMD だけの計算は理由をつけて止まるかを確かめる。

    ~/.venv/gpumd312/bin/python 01_test/test_cpu_nep.py
"""
import os
import shutil
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data_cpu_nep"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir()
os.environ.update({"MATSIM_DATA_DIR": str(DATA), "MATSIM_GPUMD_BIN": "/nonexistent/gpumd", "MATSIM_MD_ENGINE": "auto"})

from matsimapi import mcp_server as m, settings  # noqa: E402

failed = []


def check(label: str, ok: bool, detail="") -> None:
    print(f"{'OK' if ok else 'NG'}  {label}" + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        failed.append(label)


check("GPUMD がないので CPU で計算する", settings.cpu_nep() and m.get_system_status()["engine"]["md"] == "cpu")
nep = next(p for p in m.list_potentials() if "NEP89" in p["name"])
cu = m.add_sample_structure("Cu")["id"]
stages = [{"ensemble": "nvt", "steps": 600, "temperature": 300}]
preview = m.preview_calculation("md", cu, nep["id"], stages=stages, repeat=[3, 3, 3], traj_interval=100)
check("見積もりで CPU と知らせる", any("CPU" in n for n in preview["notes"]), preview["notes"])
made = m.submit_calculation("md", cu, nep["id"], stages=stages, repeat=[3, 3, 3], traj_interval=100)
job = made["jobs"][0]["id"]
done = m.wait_for_job(job, 600)
check("CPU の NEP で MD が完了", done["status"] == "done", done["message"])
thermo = m.get_job_thermo_summary(job)
check("温度が目標の近く", abs(thermo[0]["temperature_mean"] - 300) < 80, round(thermo[0]["temperature_mean"]))
check("構造の時間変化", len(m.get_job_trajectory(job)["frames"]) >= 2)
check("最終構造を登録できる", m.save_final_structure(job)["id"] > 0)
for kind, options in (("static", {"relax": True, "elastic": True}), ("phonon", {})):
    made = m.submit_calculation(kind, cu, nep["id"], options=options)
    done = m.wait_for_job(made["jobs"][0]["id"], 600)
    notes = m.get_job_results(made["jobs"][0]["id"]).get("notes", [])
    check(f"CPU の NEP で{kind}が完了", done["status"] == "done" and any("CPU" in n for n in notes), done["message"])
for kind in ("diffusion", "thermal"):
    try:
        m.preview_calculation(kind, cu, nep["id"])
        check(f"{kind} は GPUMD が要ると止まる", False)
    except Exception as exc:
        check(f"{kind} は GPUMD が要ると止まる", "GPUMD が要ります" in str(exc))
shutil.rmtree(DATA, ignore_errors=True)
print("すべて OK" if not failed else f"NG: {failed}")
sys.exit(1 if failed else 0)
