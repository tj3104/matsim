"""Atomsk のオプションをつないで 1 回の呼び出しで実行する Pipeline。

    from atomsk_kit import Pipeline, box
    out = (Pipeline(cu).duplicate(8, 8, 8)
           .dislocation(box(0.501), box(0.501), "screw", xi="z", n="y", b=2.556)
           .select_sphere(True, box(0.5), box(0.5), box(0.5), 6).remove_selected()
           .run())

位置は Å の数値か、box(割合) (= Atomsk の "割合*box"。セルの長さに対する割合) で渡す。
軸は "x" / "y" / "z"、結晶方位はミラー指数の文字列 ("[11-2]" や "11-2") で渡す。
"""
from __future__ import annotations

from ase import Atoms

from .runner import OUT, run

AXES = ("x", "y", "z")


def box(fraction: float) -> str:
    """セルの長さに対する割合で位置を指定する (Atomsk の "0.5*box" の書き方)。"""
    return f"{float(fraction):.6g}*box"


def miller(text: str) -> str:
    """ミラー指数を Atomsk の書き方 ([hkl]) にそろえる。"""
    text = str(text).replace(" ", "")
    return text if text.startswith("[") else f"[{text}]"


def _axis(value: str, allowed=AXES) -> str:
    value = str(value).lower()
    if value not in allowed:
        raise ValueError(f"軸は {' / '.join(allowed)} のどれかです: {value}")
    return value


class Pipeline:
    """構造 1 つに Atomsk のオプションを順にかける。メソッドは自分を返すのでつなげて書ける。"""

    def __init__(self, atoms: Atoms):
        self.atoms = atoms
        self.args: list = []
        self.files: dict[str, bytes | str] = {}
        self.log: list[str] = []

    def option(self, *args) -> "Pipeline":
        """まだメソッドのないオプションをそのまま足す。例: .option("-sort", "species", "up")"""
        self.args += list(args)
        return self

    def run(self, log: list[str] | None = None) -> Atoms:
        """まとめて 1 回 Atomsk を動かして結果を返す。警告は self.log (と log) に入る。"""
        result = run(["in.vasp", *self.args, OUT], inputs={"in.vasp": self.atoms}, files=self.files, log=self.log)
        if log is not None:
            log += self.log
        return result

    # ---- セル -------------------------------------------------------------------------
    def duplicate(self, nx: int, ny: int, nz: int):
        return self.option("-duplicate", int(nx), int(ny), int(nz))

    def orient(self, old: tuple[str, str, str], new: tuple[str, str, str]):
        """結晶方位 old (x, y, z) を new に回す。セルの形は変えない (orthogonal_cell と組み合わせる)。"""
        return self.option("-orient", *[miller(v) for v in (*old, *new)])

    def orthogonal_cell(self):
        return self.option("-orthogonal-cell")

    def unskew(self):
        return self.option("-unskew")

    def reduce_cell(self, axis: str | None = None):
        """周期を保って短いセルにする。axis="p" で基本単位胞を探す。"""
        return self.option("-reduce-cell", *([_axis(axis, (*AXES, "p"))] if axis else []))

    def rebox(self):
        return self.option("-rebox")

    def alignx(self):
        return self.option("-alignx")

    def wrap(self):
        return self.option("-wrap")

    def cell(self, component: str, op: str, value: float):
        """セルを変える (原子は動かさない)。例: cell("z", "add", 15) で z 方向に 15 Å の真空層。"""
        if op not in ("add", "rm", "set"):
            raise ValueError("op は add / rm / set です")
        return self.option("-cell", component, op, value)

    def center(self, index: int | str = "com"):
        return self.option("-center", index)

    # ---- 変形・移動 -----------------------------------------------------------------------
    def deform(self, component: str, strain_percent: float, poisson: float | None = None):
        """一軸ひずみ (x/y/z) またはせん断 (xy など)。ひずみは % で渡す。"""
        args = ["-deform", component, f"{strain_percent}%"]
        if poisson is not None and component in AXES:
            args.append(poisson)
        return self.option(*args)

    def rotate(self, axis: str, angle: float, com: bool = True):
        """原子を回す (Atomsk -rotate)。セルは軸にそろったまま残ることがあるので、周期的な結晶を回すのには使わない
        (結晶ごと回すなら atomsk_kit.rotate。切り出す前の大きな塊を回すときなどに使う)。"""
        return self.option("-rotate", *(["com"] if com else []), _axis(axis), angle)

    def shift(self, dx: float, dy: float, dz: float):
        return self.option("-shift", dx, dy, dz)

    def mirror(self, d, normal: str):
        return self.option("-mirror", d, normal)

    def swap(self, a: str, b: str):
        """軸どうし ("x", "z") または元素どうし ("Mg", "O") を入れ替える。"""
        return self.option("-swap", a, b)

    def torsion(self, axis: str, angle: float):
        return self.option("-torsion", _axis(axis), angle)

    def roll(self, direction: str, angle: float, axis: str):
        if _axis(direction) == _axis(axis):
            raise ValueError("曲げる方向と曲げの軸は別の軸にしてください")
        return self.option("-roll", direction, angle, axis)

    # ---- 選択と削除 -----------------------------------------------------------------------
    def cut(self, side: str, d, normal: str):
        """平面より上 (above) または下 (below) の原子を消す。"""
        if side not in ("above", "below"):
            raise ValueError("side は above / below です")
        return self.option("-cut", side, d, normal)

    def select_sphere(self, inside: bool, x, y, z, radius: float):
        return self.option("-select", "in" if inside else "out", "sphere", x, y, z, radius)

    def select_cylinder(self, inside: bool, axis: str, p1, p2, radius: float):
        """円柱。p1, p2 は軸に垂直な 2 方向の位置 (軸が z なら x, y / x なら y, z / y なら z, x)。"""
        return self.option("-select", "in" if inside else "out", "cylinder", _axis(axis), p1, p2, radius)

    def select_box(self, inside: bool, lo: tuple, hi: tuple):
        return self.option("-select", "in" if inside else "out", "box", *lo, *hi)

    def select_stl(self, stl: bytes | str, placement: str = "rescale"):
        """STL の立体の中を選ぶ。placement: asis (そのまま) / center / rescale / fill。"""
        keyword = {"asis": [], "center": ["center"], "rescale": ["rescale"], "fill": ["fill"]}[placement]
        self.files["shape.stl"] = stl
        return self.option("-select", "stl", *keyword, "shape.stl")

    def select_species(self, species: str):
        return self.option("-select", species)

    def select_invert(self):
        return self.option("-select", "invert")

    def select_all(self):
        return self.option("-select", "all")

    def remove_selected(self):
        return self.option("-remove-atom", "select")

    def remove_species(self, species: str):
        return self.option("-remove-atom", species)

    def remove_doubles(self, distance: float):
        return self.option("-remove-doubles", distance)

    def separate(self, distance: float, shift: float):
        return self.option("-separate", distance, shift)

    def substitute(self, old: str, new: str):
        """old をすべて new に置き換える (一部だけなら atomsk_kit.substitute を使う)。"""
        return self.option("-substitute", old, new)

    # ---- 欠陥 -------------------------------------------------------------------------
    def dislocation(self, p1, p2, kind: str, xi: str, n: str, b: float, nu: float | None = None):
        """直線転位。kind: screw / edge / edge_add / edge_rm。刃状はポアソン比 nu が要る。"""
        if _axis(xi) == _axis(n):
            raise ValueError("転位線の方向と切断面の法線は別の軸にしてください")
        if kind not in ("screw", "edge", "edge_add", "edge_rm"):
            raise ValueError("kind は screw / edge / edge_add / edge_rm です")
        if kind != "screw" and nu is None:
            raise ValueError("刃状転位にはポアソン比 nu が要ります")
        return self.option("-dislocation", p1, p2, kind, xi, n, b, *([nu] if kind != "screw" else []))

    def dislocation_loop(self, x, y, z, normal: str, radius: float, burgers: tuple[float, float, float], nu: float):
        return self.option("-dislocation", "loop", x, y, z, _axis(normal), radius, *burgers, nu)

    def crack(self, mode: str, plane: str, k: float, p1, p2, xi: str, n: str, mu: float, nu: float):
        """き裂。mode: I / II / III、plane: strain (平面ひずみ) / stress (平面応力)、k [GPa·Å^½]、mu [GPa]。"""
        if _axis(xi) == _axis(n):
            raise ValueError("き裂先端の線と切断面の法線は別の軸にしてください")
        return self.option("-crack", mode, plane, k, p1, p2, xi, n, mu, nu)
