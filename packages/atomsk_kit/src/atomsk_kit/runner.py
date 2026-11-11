"""Atomsk の実行ファイルを呼ぶ部分。

一時ディレクトリに入力 (VASP 形式) を書き、Atomsk を動かして出力 (XSF 形式) を ASE で読む。
POSCAR での書き出しは元素が複数だと Atomsk (0.13 系) が落ちることがあるので、出力は XSF に固定している。

    from atomsk_kit import configure, run
    configure(binary="~/repos/atomsk/src/atomsk", timeout=300)
    atoms = run(["in.vasp", "-duplicate", 2, 2, 2, OUT], inputs={"in.vasp": cu})
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ase import Atoms
from ase.io import read as ase_read
from ase.io import write as ase_write

OUT = "out.xsf"   # 引数の中でこの名前が出力ファイルになる


class AtomskError(RuntimeError):
    """Atomsk が見つからない・失敗した・時間内に終わらなかった。"""


def _default_binary() -> Path | None:
    env = os.environ.get("ATOMSK_BIN")
    if env:
        return Path(env).expanduser()
    found = shutil.which("atomsk")
    if found:
        return Path(found)
    built = Path.home() / "repos" / "atomsk" / "src" / "atomsk"
    return built if built.is_file() else None


@dataclass
class Config:
    binary: Path | None = field(default_factory=_default_binary)
    timeout: float = 300.0          # 1 回の呼び出しの上限 [秒]
    workdir: Path | None = None     # 一時ディレクトリを作る場所 (None なら OS の既定)


config = Config()


def configure(*, binary: str | Path | None = None, timeout: float | None = None,
              workdir: str | Path | None = None) -> Config:
    """実行ファイルの場所・時間の上限・一時ディレクトリの場所を変える。"""
    if binary is not None:
        config.binary = Path(binary).expanduser()
    if timeout is not None:
        config.timeout = float(timeout)
    if workdir is not None:
        config.workdir = Path(workdir).expanduser()
    return config


def available() -> bool:
    return config.binary is not None and config.binary.is_file() and os.access(config.binary, os.X_OK)


def version() -> str | None:
    """git でビルドしたものならコミットの説明 (例: Beta-0.13-59-gba5b453)。"""
    if not available():
        return None
    try:
        out = subprocess.run(["git", "-C", str(config.binary.parent.parent), "describe", "--tags"],
                             capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def write_vasp(path: Path, atoms: Atoms) -> None:
    ase_write(path, atoms, format="vasp", direct=False, vasp5=True, sort=True)


def run(args: list, inputs: dict[str, Atoms] | None = None, files: dict[str, bytes | str] | None = None,
        log: list[str] | None = None) -> Atoms:
    """Atomsk を 1 回動かして出力の構造を返す。

    args    コマンドライン引数 (数値もそのまま渡せる)。出力ファイルは OUT と書く。
    inputs  {ファイル名: 構造}。VASP 形式で書いてから呼ぶ (拡張子は .vasp にする)。
    files   {ファイル名: 中身}。パラメータファイルや STL など。
    log     渡すと Atomsk の警告 (/!\\) を書き足す。
    """
    if not available():
        raise AtomskError(f"Atomsk が見つかりません ({config.binary})。configure(binary=...) で指定してください")
    if config.workdir is not None:
        config.workdir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="atomsk-", dir=config.workdir))
    try:
        for name, item in (inputs or {}).items():
            write_vasp(run_dir / name, item)
        for name, content in (files or {}).items():
            (run_dir / name).write_bytes(content if isinstance(content, bytes) else content.encode())
        command = [str(config.binary), *[str(a) for a in args], "-ignore"]
        try:
            result = subprocess.run(command, cwd=run_dir, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                    timeout=config.timeout)
        except subprocess.TimeoutExpired:
            raise AtomskError(f"Atomsk が {config.timeout:.0f} 秒で終わりませんでした。大きさを見直してください") from None
        lines = (result.stdout + result.stderr).splitlines()
        errors = [line.replace("X!X", "").replace("ERROR:", "").strip(" |") for line in lines if "X!X" in line]
        output = run_dir / OUT
        if result.returncode != 0 or not output.is_file() or errors:
            detail = " / ".join(e for e in errors[:3] if e) or (lines[-1].strip() if lines else "原因不明")
            raise AtomskError(f"Atomsk でエラーになりました: {detail}")
        if log is not None:
            for line in lines:
                if "/!\\" in line:
                    log.append("Atomsk: " + line.replace("/!\\", "").replace("WARNING:", "").strip())
        atoms = ase_read(output, format="xsf")
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    atoms.pbc = True
    if len(atoms) == 0:
        raise AtomskError("原子が 1 つも残りませんでした。条件を見直してください")
    return atoms
