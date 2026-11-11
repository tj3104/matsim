"""計算の種類ごとに、小さな系で実際に GPUMD を動かして結果の取り出しまで確認する。

    ~/.venv/gpumd312/bin/python 01_test/try_kinds.py [種類 ...]
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from gpumd_toolkit.structure import StructureHandler  # noqa: E402

from matsimapi import kinds, potentials, settings, structures  # noqa: E402

NEP = next(iter(settings.BUILTIN_POTENTIALS.values()))
POT = {"path": str(NEP), "elements": potentials.parse_elements(NEP), "name": "NEP89"}
OUT = ROOT / "01_test" / "kinds_run"
CU, MGO = "Cu", "MgO"
CASES = {
    "static": (CU, (2, 2, 2), {}),
    "diffusion": (MGO, (3, 3, 3), {"temperature": 2000, "equil_steps": 500, "steps": 2000, "ic_element": "O", "ic_charge": -2, "correlation": 100}),
    "thermal": (CU, (4, 4, 4), {"equil_steps": 500, "steps": 3000, "output_interval": 100}),
    "thermal_gk": (CU, (4, 4, 4), {"method": "gk", "equil_steps": 500, "steps": 3000, "correlation_steps": 100}),
    "free_energy": (CU, (3, 3, 3), {"equil_steps": 500, "t_switch": 1000, "t_equil": 300, "scan_switch": 1000, "T_end": 600}),
    "free_energy_liquid": (CU, (3, 3, 3), {"phase": "liquid", "temperature": 3000, "equil_steps": 500, "t_switch": 1000, "t_equil": 300, "scan": False}),
    "tensile": (CU, (4, 4, 4), {"equil_steps": 500, "steps": 3000, "strain_rate": 2e10}),
    "mc": (MGO, (5, 5, 5), {"steps": 1000, "md_steps": 100, "mc_trials": 50}),
    "mc_mcmc": (MGO, (5, 5, 5), {"mode": "mcmc", "trials": 5000, "trials_per_call": 500}),
    "pimd": (CU, (3, 3, 3), {"steps": 1000, "num_beads": "4"}),
    "pimd_qtb": (CU, (3, 3, 3), {"method": "qtb", "steps": 1000}),
    "shock": (CU, (4, 4, 4), {"equil_steps": 500, "steps": 2000}),
    "ttm": (CU, (3, 3, 6), {"equil_steps": 500, "steps": 2000, "out_interval": 100}),
    "al": (CU, (3, 3, 3), {"steps": 1000, "threshold": 0.0}),
}


def context(kind, atoms, repeat, raw, workdir):
    return kinds.Context(atoms=atoms, potential=POT, potentials=[POT, POT] if kind.committee else [POT],
                         workdir=workdir, repeat=repeat, time_step=1.0, thermo_interval=100, traj_interval=500,
                         o=kinds.validate_options(kind.fields, raw))


def run(name):
    sample, repeat, raw = CASES[name]
    kind = kinds.get(name.split("_")[0] if name.split("_")[0] in kinds.KINDS else "free_energy")
    workdir = OUT / name
    shutil.rmtree(workdir, ignore_errors=True)
    atoms, _ = structures.sample(sample)
    calc, notes = kinds.build(kind, context(kind, atoms, repeat, raw, workdir))
    calc.write_inputs()
    start = time.time()
    with open(workdir / "gpumd.out", "w") as out, open(workdir / "gpumd.err", "w") as err:
        rc = subprocess.run([str(settings.GPUMD_BIN)], cwd=workdir, stdout=out, stderr=err).returncode
    finished = "Finished running GPUMD." in (workdir / "gpumd.out").read_text()
    print(f"\n=== {name}: N={calc.n_atoms} steps={calc.builder.total_steps} rc={rc} finished={finished} "
          f"{time.time() - start:.1f}s notes={notes}")
    if not finished:
        print("   ERR:", (workdir / "gpumd.err").read_text()[-400:] or (workdir / "gpumd.out").read_text()[-400:])
        return False
    # サーバーの後処理と同じく、model.xyz から計算を組み立て直して結果を取り出す
    model = structures.clean(StructureHandler.read_model(workdir / "model.xyz"))
    ctx = context(kind, model, (1, 1, 1), raw, workdir)
    again, _ = kinds.build(kind, ctx)
    out = kinds.Collected()
    kind.collect(again, ctx, out)
    for h in out.highlights:
        print(f"   {h['label']}: {h['value']} {h['unit']}")
    for d in out.datasets:
        print(f"   [{d.key}] {d.title}: rows={len(d.frame)} x={d.x} y={d.y} cols={list(d.frame.columns)[:10]}")
    print("   notes:", out.notes)
    print("   files:", sorted(p.name for p in workdir.iterdir())[:30])
    return not any("読めません" in n for n in out.notes)


if __name__ == "__main__":
    names = sys.argv[1:] or list(CASES)
    results = {name: run(name) for name in names}
    print("\n", results)
