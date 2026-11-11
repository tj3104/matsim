"""GPUMD_toolkit の不具合報告 (toolkit_handoff.md) の再現スクリプト。

    ~/.venv/gpumd312/bin/python 01_test/toolkit_repro.py

01_test/repro/ に小さな計算 (Cu・MgO、NEP89、数千ステップ) を作って実行し、各項目の挙動を表示する。
"""
import shutil
from pathlib import Path

import gpumd_toolkit
from ase.build import bulk
from gpumd_toolkit.config import GPUMDEnvironment
from gpumd_toolkit.workflows import DiffusionCalculation, FreeEnergyCalculation, ThermalTransport

NEP = Path.home() / "repos/GPUMD-Tutorials/benchmark/01_CuMoTaVW/nep89_20250409.txt"
OUT = Path(__file__).resolve().parent / "repro"
ENV = GPUMDEnvironment(venv=None, use_login_shell=False, gpumd_command=str(Path.home() / "repos/GPUMD/src/gpumd"))
CU = bulk("Cu", cubic=True)
MGO = bulk("MgO", "rocksalt", a=4.212, cubic=True)

shutil.rmtree(OUT, ignore_errors=True)

print("== B1: バージョン表記")
print("   gpumd_toolkit.__version__ =", gpumd_toolkit.__version__, "(pyproject.toml は 0.4.3)")

print("== B2: 元素別 MSD のあとの diffusion_coefficient()")
calc = DiffusionCalculation(MGO, NEP, OUT / "diffusion", repeat=(3, 3, 3), environment=ENV)
calc.group_by_species()
calc.equilibrate(temperature=2000, steps=300)
calc.production(temperature=2000, steps=1500, msd=True, per_species=True, Nc=100)
calc.run(check=False)
print("   msd.out の列:", list(calc.msd().columns)[:7], "...")
for kwargs in ({}, {"group": 0}):
    try:
        print(f"   diffusion_coefficient({kwargs}) ->", calc.diffusion_coefficient(**kwargs))
    except Exception as exc:
        print(f"   diffusion_coefficient({kwargs}) -> {type(exc).__name__}: {exc}")

print("== Q1: hysteresis() の値 (固体と液体)")
solid = FreeEnergyCalculation(CU, NEP, OUT / "frenkel_ladd", repeat=(3, 3, 3), environment=ENV)
solid.equilibrate(temperature=300, steps=500, pressure=0.0)
solid.frenkel_ladd(temperature=300, t_equil=300, t_switch=1000)
solid.run(check=False)
print("   frenkel_ladd   :", solid.hysteresis())
liquid = FreeEnergyCalculation(CU, NEP, OUT / "uhlenbeck_ford", repeat=(3, 3, 3), environment=ENV)
liquid.equilibrate(temperature=3000, steps=500, pressure=0.0)
liquid.uhlenbeck_ford(temperature=3000, t_equil=300, t_switch=1000)
liquid.run(check=False)
print("   uhlenbeck_ford :", liquid.hysteresis())

print("== Q2: HNEMD (direction='x') の thermal_conductivity()")
hnemd = ThermalTransport(CU, NEP, OUT / "hnemd", repeat=(4, 4, 4), environment=ENV)
hnemd.equilibrate(temperature=300, steps=300)
hnemd.hnemd(temperature=300, steps=2000, direction="x", output_interval=100)
hnemd.run(check=False)
print("   ", hnemd.thermal_conductivity())
