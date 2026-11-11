"""構造のモデリング。置換・空孔・ランダム合金などの原子の編集、Atomsk の各機能、グラニュラー構造。

操作 (Op) ごとに「入力欄の定義 (fields)」と「構造を組み立てる関数 (build)」を持つ。
画面のフォームは fields から自動で作る (計算の種類 kinds.py と同じ仕組み) ので、
操作を足すときはここに Op を 1 つ追加すればよい。

構造を組み立てる中身は atomsk_kit (packages/atomsk_kit) にあり、ここは画面の入力をキットの引数に直すだけ。
乱数を使う操作はすべて「乱数のシード」を入力に持ち、同じ入力なら同じ構造になる。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Callable

import atomsk_kit as ak
from ase import Atoms
from atomsk_kit import box

from . import settings
from .kinds import _visible, choice, flag, num, ptext, validate_options

ak.configure(binary=settings.ATOMSK_BIN, timeout=settings.ATOMSK_TIMEOUT, workdir=settings.TMP_DIR)

AXIS3 = [{"value": a, "label": a} for a in "xyz"]
VALID_SYMBOLS = ak.geometry.VALID_SYMBOLS
MILLER = r"\[?-?\d+(?:\s*-?\d+)*\]?"          # 例: [11-2] / 110 / [2-1-10]
COMPOSITION = r"[A-Z][a-z]?\s*:\s*\d+(?:\.\d+)?(?:\s*,\s*[A-Z][a-z]?\s*:\s*\d+(?:\.\d+)?)*"
SPECIES_LIST = r"[A-Z][a-z]?(?:\s+[A-Z][a-z]?)*"


# ---- 入力欄の定義 (kinds.py の num / choice / flag に加えて使うもの) -------------------------

def species(key, label, default=None, *, source="structure", allow_all=False, hint="", advanced=False,
            show_if=None) -> dict:
    """元素。source="structure" は元の構造にある元素から選ぶ、"any" は自由に入力する。"""
    return {"key": key, "label": label, "type": "species", "default": default, "source": source,
            "allow_all": allow_all, "hint": hint, "advanced": advanced, "show_if": show_if}


def structure_ref(key, label, *, hint="", show_if=None, optional=False) -> dict:
    """登録済みの別の構造。"""
    return {"key": key, "label": label, "type": "structure", "default": None, "hint": hint, "advanced": False,
            "show_if": show_if, "optional": optional}


def upload(key, label, *, accept, hint="", max_bytes=settings.MAX_STL_BYTES) -> dict:
    """ファイル。画面からは {"name", "data" (base64)} で届く。"""
    return {"key": key, "label": label, "type": "file", "default": None, "accept": accept, "hint": hint,
            "advanced": False, "show_if": None, "max_bytes": max_bytes}


def frac(key, label, default, **kwargs) -> dict:
    return num(key, label, default, min=0, max=1, hint=kwargs.pop("hint", "セルに対する割合 (0〜1)"), **kwargs)


def seed_field() -> dict:
    return num("seed", "乱数のシード", 1, min=0, max=2**31 - 1, integer=True, advanced=True,
               hint="同じシードなら同じ構造になります")


def axis_field(key, label, default, **kwargs) -> dict:
    return choice(key, label, default, AXIS3, **kwargs)


def amount_fields(default_mode="percent", default_count=1, default_percent=10.0) -> list[dict]:
    return [
        choice("amount", "量の指定", default_mode, [
            {"value": "count", "label": "個数"}, {"value": "percent", "label": "割合 (%)"},
            {"value": "all", "label": "すべて"}]),
        num("count", "個数", default_count, min=1, max=settings.MAX_ATOMS, integer=True, show_if={"amount": "count"}),
        num("percent", "割合", default_percent, min=0, max=100, unit="%", show_if={"amount": "percent"}),
    ]


def validate(fields: list[dict], raw: dict, atoms: Atoms | None) -> dict:
    """画面から来た値を検証して既定値を補う。数値・選択肢は kinds.validate_options に任せる。"""
    base = [f for f in fields if f["type"] in ("number", "int", "select", "bool", "text")]
    values = validate_options(base, raw)
    present = sorted(set(atoms.get_chemical_symbols())) if atoms is not None else []
    for spec in fields:
        if spec in base:
            continue
        key, kind = spec["key"], spec["type"]
        if not _visible(spec, values):
            values[key] = spec["default"]
            continue
        value = raw.get(key, spec["default"])
        if kind == "species":
            value = str(value or "").strip()
            if spec["allow_all"] and value in ("", "*"):
                values[key] = "*"
                continue
            if not value and spec["source"] == "structure" and present:
                value = present[0]
            value = value[:1].upper() + value[1:].lower()
            if value not in VALID_SYMBOLS:
                raise ValueError(f"{spec['label']} の元素記号が正しくありません")
            if spec["source"] == "structure" and value not in present:
                raise ValueError(f"{spec['label']}: 元の構造に {value} がありません")
        elif kind == "structure":
            if value in (None, ""):
                if spec.get("optional"):
                    values[key] = None
                    continue
                raise ValueError(f"{spec['label']} を選んでください")
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{spec['label']} の選択が正しくありません") from None
        elif kind == "file":
            if not isinstance(value, dict) or not value.get("data"):
                raise ValueError(f"{spec['label']} を選んでください")
            try:
                data = base64.b64decode(str(value["data"]), validate=True)
            except ValueError:
                raise ValueError(f"{spec['label']} を読めませんでした") from None
            if len(data) > spec["max_bytes"]:
                raise ValueError(f"{spec['label']} が大きすぎます (最大 {spec['max_bytes'] // 1024 // 1024}MB)")
            value = data
        values[key] = value
    return values


# ---- 組み立て (atomsk_kit を呼ぶ) ---------------------------------------------------------

@dataclass
class Ctx:
    atoms: Atoms | None             # 元の構造 (新しく作る操作では None)
    o: dict                         # 検証済みの入力
    others: dict[str, Atoms]        # 入力欄で選んだ別の構造 (key → 構造)
    warnings: list[str] = field(default_factory=list)

    @property
    def seed(self) -> int:
        return int(self.o.get("seed", 1))

    def need_atoms(self) -> Atoms:
        if self.atoms is None:
            raise ValueError("元の構造を選んでください")
        return self.atoms.copy()

    def pipe(self) -> ak.Pipeline:
        return ak.Pipeline(self.need_atoms())


def atomsk_available() -> bool:
    return ak.available()


def amount(o: dict) -> dict:
    """「量の指定」欄を atomsk_kit の count / fraction に直す。"""
    if o["amount"] == "count":
        return {"count": o["count"]}
    if o["amount"] == "percent":
        return {"fraction": o["percent"] / 100}
    return {}


def pipe(steps: Callable[[ak.Pipeline, dict], ak.Pipeline]) -> Callable[[Ctx], Atoms]:
    """元の構造に Pipeline のオプションをかけるだけの操作。"""
    def build(c: Ctx) -> Atoms:
        return steps(c.pipe(), c.o).run(log=c.warnings)
    return build


def removed(c: Ctx, atoms: Atoms) -> Atoms:
    c.warnings.append(f"{len(c.atoms) - len(atoms)} 個の原子を取り除きました")
    return atoms


def b_create(c: Ctx) -> Atoms:
    o = c.o
    return ak.create(o["lattice"], o["a"], o["elements"], c=o["c"] if o["lattice"] in NEEDS_C else None,
                     orient=(o["ox"], o["oy"], o["oz"]) if o["orient"] else None,
                     duplicate=(o["nx"], o["ny"], o["nz"]), log=c.warnings)


NEEDS_C = [k for k, v in ak.LATTICES.items() if v[1]]
TIDY = {"wrap": lambda p: p.wrap(), "unskew": lambda p: p.unskew(), "reduce": lambda p: p.reduce_cell(),
        "primitive": lambda p: p.reduce_cell("p"), "rebox": lambda p: p.rebox(), "alignx": lambda p: p.alignx()}


def orient(p: ak.Pipeline, o: dict) -> ak.Pipeline:
    p.orient((o["fx"], o["fy"], o["fz"]), (o["tx"], o["ty"], o["tz"]))
    return p.orthogonal_cell() if o["ortho"] else p


def b_vacuum(c: Ctx) -> Atoms:
    p = c.pipe().cell(c.o["axis"], "add", c.o["vacuum"])
    return (p.center() if c.o["center"] else p).run(log=c.warnings)


def b_vacuum_all(c: Ctx) -> Atoms:
    """分子・クラスターを、全方位に真空を空けた直方体の箱の中央に置く (周期の結晶は周期が切れる)。"""
    from .pubchem import boxed
    atoms = boxed(c.need_atoms(), c.o["vacuum"])
    c.warnings.append("新しい直方体の箱にしました。結晶だった場合、周期のつながりは切れます")
    return atoms


def b_place_molecule(c: Ctx) -> Atoms:
    """分子 (別の構造) を、向きを回してから指定の位置 (空欄ならセルの中央) に置き、元の構造に加える。"""
    import numpy as np
    host = c.need_atoms()
    molecule = c.others.get("molecule")
    if molecule is None:
        raise ValueError("置く分子を選んでください")
    molecule = molecule.copy()
    molecule.set_cell([0, 0, 0])
    molecule.pbc = False
    molecule.positions -= molecule.positions.mean(axis=0)   # 分子の中心を原点に
    for axis in ("x", "y", "z"):
        angle = c.o.get(f"r{axis}") or 0
        if angle:
            molecule.rotate(angle, axis, center=(0, 0, 0))
    position = [c.o.get(k) for k in ("x", "y", "z")]
    middle = host.cell.sum(axis=0) / 2
    molecule.positions += np.array([middle[i] if v is None else v for i, v in enumerate(position)])
    if c.o.get("rmd"):   # 分子と重なる元の構造の原子を消す
        from ase.geometry import get_distances
        _, distances = get_distances(host.positions, molecule.positions, cell=host.cell, pbc=host.pbc)
        keep = distances.min(axis=1) >= c.o["rmd"]
        if (~keep).any():
            c.warnings.append(f"分子と重なる元の構造の原子を {int((~keep).sum())} 個消しました")
        host = host[keep]
    combined = host + Atoms(molecule.get_chemical_symbols(), positions=molecule.positions)
    combined.cell, combined.pbc = host.cell, host.pbc
    return combined


def cylinder_center(axis: str, cx: float, cy: float, cz: float) -> tuple[float, float]:
    """Atomsk の cylinder は軸に垂直な 2 方向の座標を X→Y→Z の順の入れ替えで受け取る。"""
    return {"z": (cx, cy), "x": (cy, cz), "y": (cz, cx)}[axis]


def b_region(c: Ctx) -> Atoms:
    o, p = c.o, c.pipe()
    inside = o["action"] != "keep"      # 「中を残す」は外側を選んで消す
    if o["shape"] == "sphere":
        p.select_sphere(inside, box(o["cx"]), box(o["cy"]), box(o["cz"]), o["radius"])
    elif o["shape"] == "cylinder":
        p1, p2 = cylinder_center(o["axis"], o["cx"], o["cy"], o["cz"])
        p.select_cylinder(inside, o["axis"], box(p1), box(p2), o["radius"])
    else:
        lo = [box(max(0.0, o[k] - o[k.replace("c", "h")] / 2)) for k in ("cx", "cy", "cz")]
        hi = [box(min(1.0, o[k] + o[k.replace("c", "h")] / 2)) for k in ("cx", "cy", "cz")]
        p.select_box(inside, lo, hi)
    return removed(c, p.remove_selected().run(log=c.warnings))


def b_stl(c: Ctx) -> Atoms:
    p = c.pipe().select_stl(c.o["stl"], c.o["placement"])
    if c.o["action"] == "keep":
        p.select_invert()
    return removed(c, p.remove_selected().run(log=c.warnings))


def b_dislocation(c: Ctx) -> Atoms:
    o = c.o
    return c.pipe().dislocation(box(o["p1"]), box(o["p2"]), o["dtype"], o["xi"], o["normal"], o["b"],
                                None if o["dtype"] == "screw" else o["nu"]).run(log=c.warnings)


def b_merge(c: Ctx) -> Atoms:
    o = c.o
    if o["mode"] == "stack" and o["scale"]:
        note = _stack_scale_note(c.need_atoms(), c.others["other"], o["axis"])
        if note:
            c.warnings.append(note)
    return ak.merge([c.need_atoms(), c.others["other"]], stack=o["axis"] if o["mode"] == "stack" else None,
                    scale=o["mode"] == "stack" and o["scale"], remove_doubles=o["rmd"] or None, log=c.warnings)


def b_polycrystal(c: Ctx) -> Atoms:
    o = c.o
    matrix = c.others.get("matrix")
    clearance = o["clearance"] if matrix is not None or o["clearance"] > 0.1 else None
    result = ak.polycrystal(c.need_atoms(), (o["lx"], o["ly"], o["lz"]), o["grains"], columnar=o["dim"] == "2d",
                            nz=o["nz"], matrix=matrix, clearance=clearance, remove_doubles=o["rmd"] or None,
                            seed_rng=c.seed, max_atoms=settings.MAX_ATOMS, log=c.warnings)
    c.warnings.append("粒界の原子は緩和していません。MD (アニール・構造緩和) をかけてから使ってください")
    return result


def b_granular(c: Ctx) -> Atoms:
    o = c.o
    return ak.granular(
        c.need_atoms(), (o["lx"], o["ly"], o["lz"]), grains=o["grains"], radius=o["radius"], spread=o["spread"] / 100,
        gap=o["gap"], layout=o["layout"], orientation=o["orientation"], filler=o["filler"],
        fill_composition=o["fill_comp"] if o["fill_species"] == "custom" else None,
        fill_density=o["fill_density"] / 100, matrix=c.others.get("matrix"), clearance=o["clearance"], dmin=o["dmin"],
        seed=c.seed, max_atoms=settings.MAX_ATOMS, log=c.warnings)


# ---- 操作の一覧 ---------------------------------------------------------------------------

@dataclass
class Op:
    key: str
    label: str
    group: str
    note: str
    fields: list[dict]
    build: Callable[[Ctx], Atoms]
    needs_structure: bool = True
    uses_atomsk: bool = True

    def listing(self) -> dict:
        return {"key": self.key, "label": self.label, "group": self.group, "note": self.note, "fields": self.fields,
                "needs_structure": self.needs_structure, "uses_atomsk": self.uses_atomsk}


G_EDIT, G_CREATE, G_CELL, G_SHAPE, G_DEFECT, G_MICRO = (
    "原子の編集", "結晶をつくる", "セル・変形", "切り出し・形状", "欠陥・界面", "多結晶・組織")
DEFORM_COMPONENTS = [{"value": v, "label": l} for v, l in [
    ("x", "x 方向に伸縮"), ("y", "y 方向に伸縮"), ("z", "z 方向に伸縮"),
    ("xy", "せん断 xy"), ("xz", "せん断 xz"), ("yz", "せん断 yz"), ("yx", "せん断 yx"), ("zx", "せん断 zx"), ("zy", "せん断 zy")]]
SHAPES = [{"value": "sphere", "label": "球"}, {"value": "cylinder", "label": "円柱"}, {"value": "box", "label": "直方体"}]
KEEP_REMOVE = [{"value": "remove", "label": "形の中の原子を消す"}, {"value": "keep", "label": "形の中だけ残す"}]

OPS = [
    # 原子の編集
    Op("substitute", "置換", G_EDIT, "ある元素の一部 (またはすべて) を別の元素に置き換えます",
       [species("from", "置き換える元素"), species("to", "置き換え後の元素", "Ni", source="any"),
        *amount_fields(), seed_field()], lambda c: ak.substitute(c.atoms, c.o["from"], c.o["to"], **amount(c.o), seed=c.seed, log=c.warnings), uses_atomsk=False),
    Op("vacancy", "空孔", G_EDIT, "原子をランダムに取り除いて空孔を作ります",
       [species("target", "取り除く元素", "*", allow_all=True), *amount_fields("count", 1, 1.0), seed_field()],
       lambda c: ak.vacancy(c.atoms, c.o["target"], **amount(c.o), seed=c.seed, log=c.warnings), uses_atomsk=False),
    Op("random_alloy", "ランダム合金", G_EDIT, "指定したサイトを組成どおりの元素でランダムに埋めます (固溶体・高エントロピー合金)",
       [species("sites", "対象のサイト", "*", allow_all=True, hint="この元素のサイトだけを置き換えます"),
        ptext("composition", "組成 (比率)", "Cu:25,Ni:25,Co:25,Fe:25", COMPOSITION,
              hint="例: Cu:50,Ni:50 (合計は 100 でなくてもよい)"), seed_field()],
       lambda c: ak.random_alloy(c.atoms, c.o["composition"], c.o["sites"], seed=c.seed, log=c.warnings),
       uses_atomsk=False),
    Op("interstitial", "格子間原子", G_EDIT, "既存の原子から一定距離以上離れた場所に原子を加えます",
       [species("element", "加える元素", "H", source="any"),
        num("count", "個数", 1, min=1, max=5000, integer=True),
        num("min_dist", "既存の原子からの最小距離", 1.5, min=0.3, max=10, unit="Å"), seed_field()],
       lambda c: ak.interstitial(c.atoms, c.o["element"], c.o["count"], min_dist=c.o["min_dist"], seed=c.seed),
       uses_atomsk=False),
    Op("swap_species", "元素の入れ替え", G_EDIT, "2 つの元素を入れ替えます (A↔B)",
       [species("sp1", "元素 1"), species("sp2", "元素 2")], lambda c: ak.swap_species(c.atoms, c.o["sp1"], c.o["sp2"]), uses_atomsk=False),
    Op("disturb", "原子をランダムに揺らす", G_EDIT, "各原子を各方向に ±振幅 の範囲でランダムにずらします",
       [num("amplitude", "振幅", 0.05, min=0.001, max=2, unit="Å"), seed_field()],
       lambda c: ak.disturb(c.atoms, c.o["amplitude"], seed=c.seed), uses_atomsk=False),
    Op("remove_doubles", "重なった原子の削除", G_EDIT, "指定した距離より近い原子の組の片方を消します (Atomsk -remove-doubles)",
       [num("distance", "距離", 0.5, min=0.01, max=5, unit="Å")],
       lambda c: removed(c, c.pipe().remove_doubles(c.o["distance"]).run(log=c.warnings))),
    Op("separate", "近すぎる原子を離す", G_EDIT, "近すぎる原子の組を互いに遠ざけます (Atomsk -separate)",
       [num("distance", "この距離より近い組", 1.0, min=0.01, max=5, unit="Å"),
        num("shift", "ずらす量", 0.3, min=0.01, max=5, unit="Å")],
       pipe(lambda p, o: p.separate(o["distance"], o["shift"]))),

    # 結晶をつくる
    Op("create", "結晶を作る", G_CREATE, "格子の種類・格子定数・元素から単位胞やスーパーセルを作ります (Atomsk --create)",
       [choice("lattice", "格子", "fcc", [{"value": k, "label": v[0]} for k, v in ak.LATTICES.items()]),
        num("a", "格子定数 a", 3.615, min=0.5, max=100, unit="Å"),
        num("c", "格子定数 c", 5.0, min=0.5, max=100, unit="Å", show_if={"lattice": NEEDS_C}),
        ptext("elements", "元素", "Cu", SPECIES_LIST, hint="空白区切り。例: Cu / Na Cl / Ga N / Sr Ti O"),
        flag("orient", "結晶方位を指定する", False, hint="立方晶・六方晶のみ"),
        ptext("ox", "x 方向", "[11-2]", MILLER, show_if={"orient": True}, hint="ミラー指数。例: [11-2]"),
        ptext("oy", "y 方向", "[1-10]", MILLER, show_if={"orient": True}),
        ptext("oz", "z 方向", "[111]", MILLER, show_if={"orient": True}),
        num("nx", "繰り返し x", 1, min=1, max=settings.MAX_REPEAT, integer=True),
        num("ny", "繰り返し y", 1, min=1, max=settings.MAX_REPEAT, integer=True),
        num("nz", "繰り返し z", 1, min=1, max=settings.MAX_REPEAT, integer=True)],
       b_create, needs_structure=False),
    Op("nanotube", "ナノチューブ", G_CREATE, "カイラル指数 (m, n) のナノチューブを作ります (Atomsk --create nanotube)",
       [num("a0", "格子定数 (グラフェン)", 2.46, min=0.5, max=10, unit="Å"),
        num("m", "m", 5, min=0, max=50, integer=True), num("n", "n", 5, min=0, max=50, integer=True),
        ptext("elements", "元素", "C", SPECIES_LIST, hint="C / B N など"),
        num("nz", "軸方向の繰り返し", 5, min=1, max=200, integer=True)],
       lambda c: ak.nanotube(c.o["a0"], c.o["m"], c.o["n"], c.o["elements"], nz=c.o["nz"], log=c.warnings),
       needs_structure=False),

    # セル・変形
    Op("supercell", "スーパーセル", G_CELL, "セルを各方向に繰り返します (Atomsk -duplicate)",
       [num("nx", "x", 2, min=1, max=settings.MAX_REPEAT, integer=True),
        num("ny", "y", 2, min=1, max=settings.MAX_REPEAT, integer=True),
        num("nz", "z", 2, min=1, max=settings.MAX_REPEAT, integer=True)],
       pipe(lambda p, o: p.duplicate(o["nx"], o["ny"], o["nz"]))),
    Op("orient", "結晶方位の変更", G_CELL,
       "立方晶の向きを変えます (Atomsk -orient)。新しい向きで周期的な直方体のセルも探せます (-orthogonal-cell)",
       [ptext("fx", "今の x", "[100]", MILLER), ptext("fy", "今の y", "[010]", MILLER), ptext("fz", "今の z", "[001]", MILLER),
        ptext("tx", "新しい x", "[11-2]", MILLER), ptext("ty", "新しい y", "[1-10]", MILLER), ptext("tz", "新しい z", "[111]", MILLER),
        flag("ortho", "新しい向きの直方体のセルにする", True, hint="外すとセルごと回転するだけ")],
       pipe(orient)),
    Op("orthogonal", "直交セルにする", G_CELL, "斜めのセルを同じ結晶の直方体のセルに作り直します (Atomsk -orthogonal-cell)",
       [], pipe(lambda p, o: p.orthogonal_cell())),
    Op("tidy", "セルを整える", G_CELL, "はみ出した原子を戻す・セルの歪みを直す・最小のセルを探すなど",
       [choice("action", "内容", "wrap", [
           {"value": "wrap", "label": "はみ出した原子をセルに戻す (-wrap)"},
           {"value": "unskew", "label": "セルの傾きを小さくする (-unskew)"},
           {"value": "reduce", "label": "周期を保って短いセルにする (-reduce-cell)"},
           {"value": "primitive", "label": "基本単位胞を探す (-reduce-cell p)"},
           {"value": "rebox", "label": "原子を包む大きさにセルを作り直す (-rebox)"},
           {"value": "alignx", "label": "a を x 軸に揃える (-alignx)"}])],
       pipe(lambda p, o: TIDY[o["action"]](p))),
    Op("vacuum", "真空層を加える (表面)", G_CELL, "セルをある方向に伸ばして真空層を作り、表面スラブにします (Atomsk -cell add)",
       [axis_field("axis", "方向", "z"), num("vacuum", "真空層の厚さ", 15, min=0.1, max=500, unit="Å"),
        flag("center", "原子をセルの中央に寄せる", True)], b_vacuum),
    Op("vacuum_all", "真空層を全方位に (分子・クラスター)", G_CELL,
       "分子やクラスターを、全方位に真空を空けた直方体の箱の中央に置きます。MD や第一原理計算を分子で行うときに使います",
       [num("vacuum", "真空層の厚さ (片側)", 10, min=0.1, max=100, unit="Å")], b_vacuum_all, uses_atomsk=False),
    Op("deform", "ひずみを加える", G_CELL, "一軸ひずみまたはせん断ひずみを加えます (Atomsk -deform)",
       [choice("component", "成分", "x", DEFORM_COMPONENTS), num("strain", "ひずみ", 2, min=-50, max=50, unit="%"),
        num("poisson", "ポアソン比", None, min=-1, max=0.5, optional=True, show_if={"component": ["x", "y", "z"]},
            hint="空欄なら横方向は変えない")],
       pipe(lambda p, o: p.deform(o["component"], o["strain"], o["poisson"]))),
    Op("rotate", "回転", G_CELL,
       "セルと原子を一緒に回転させます (結晶としては同じで、座標軸に対する向きだけが変わります)。"
       "回した結晶を別の結晶と積み重ねるには、この操作ではなく「双結晶 (粒界)」か「結晶方位の変更」を使ってください",
       [axis_field("axis", "回転軸", "z"), num("angle", "角度", 45, min=-360, max=360, unit="°"),
        flag("com", "重心のまわりに回す", True, hint="外すと原点のまわり")],
       lambda c: ak.rotate(c.need_atoms(), c.o["axis"], c.o["angle"], com=c.o["com"], log=c.warnings),
       uses_atomsk=False),
    Op("shift", "平行移動", G_CELL, "すべての原子をずらします (Atomsk -shift)",
       [num("dx", "x", 0, min=-1000, max=1000, unit="Å"), num("dy", "y", 0, min=-1000, max=1000, unit="Å"),
        num("dz", "z", 1, min=-1000, max=1000, unit="Å")],
       pipe(lambda p, o: p.shift(o["dx"], o["dy"], o["dz"]).wrap())),
    Op("mirror", "鏡映", G_CELL, "平面に対して鏡映します (Atomsk -mirror)",
       [axis_field("normal", "鏡映面の法線", "z"), frac("d", "鏡映面の位置", 0.5)],
       pipe(lambda p, o: p.mirror(box(o["d"]), o["normal"]).wrap())),
    Op("swap_axes", "軸の入れ替え", G_CELL, "2 つの座標軸を入れ替えます (Atomsk -swap)",
       [axis_field("a1", "軸 1", "x"), axis_field("a2", "軸 2", "z")],
       pipe(lambda p, o: p.swap(o["a1"], o["a2"]))),
    Op("torsion", "ねじり", G_CELL, "軸のまわりにねじります。長い棒・ワイヤー向け (Atomsk -torsion)",
       [axis_field("axis", "ねじる軸", "z"), num("angle", "端から端までの角度", 10, min=-360, max=360, unit="°")],
       pipe(lambda p, o: p.torsion(o["axis"], o["angle"]))),
    Op("roll", "曲げる (巻く)", G_CELL, "薄い板を円弧状に曲げます。360° でチューブになります (Atomsk -roll)",
       [axis_field("direction", "曲げる方向", "x"), num("angle", "角度", 90, min=0.1, max=360, unit="°"),
        axis_field("axis", "曲げの軸 (円柱の軸)", "y")],
       pipe(lambda p, o: p.roll(o["direction"], o["angle"], o["axis"]))),

    # 切り出し・形状
    Op("cut", "平面で切る", G_SHAPE, "平面より上 (または下) の原子を消します。セルの大きさは変わりません (Atomsk -cut)",
       [choice("side", "消す側", "above", [{"value": "above", "label": "平面より上"}, {"value": "below", "label": "平面より下"}]),
        axis_field("normal", "平面の法線", "z"), frac("d", "平面の位置", 0.5)],
       pipe(lambda p, o: p.cut(o["side"], box(o["d"]), o["normal"]))),
    Op("region", "形で切り抜く", G_SHAPE, "球・円柱・直方体の中 (または外) の原子を消します (Atomsk -select)",
       [choice("action", "操作", "remove", KEEP_REMOVE), choice("shape", "形", "sphere", SHAPES),
        axis_field("axis", "円柱の軸", "z", show_if={"shape": "cylinder"}),
        frac("cx", "中心 x", 0.5), frac("cy", "中心 y", 0.5), frac("cz", "中心 z", 0.5),
        num("radius", "半径", 5, min=0.1, max=10000, unit="Å", show_if={"shape": ["sphere", "cylinder"]}),
        frac("hx", "幅 x", 0.5, show_if={"shape": "box"}), frac("hy", "幅 y", 0.5, show_if={"shape": "box"}),
        frac("hz", "幅 z", 0.5, show_if={"shape": "box"})], b_region),
    Op("nanoparticle", "ナノ粒子・ナノワイヤー", G_SHAPE, "結晶から球 (粒子) または円柱 (z 方向に周期的なワイヤー) を切り出し、まわりを真空にします",
       [choice("shape", "形", "sphere", [{"value": "sphere", "label": "球 (ナノ粒子)"}, {"value": "wire", "label": "円柱 (ナノワイヤー)"}]),
        num("radius", "半径", 12, min=1, max=500, unit="Å"), num("vacuum", "まわりの真空", 10, min=0, max=500, unit="Å"),
        num("nz", "ワイヤーの長さ (z の繰り返し)", 6, min=1, max=500, integer=True, show_if={"shape": "wire"})],
       lambda c: ak.nanoparticle(c.need_atoms(), c.o["radius"], vacuum=c.o["vacuum"], shape=c.o["shape"], nz=c.o["nz"],
                                 max_atoms=settings.MAX_ATOMS, log=c.warnings)),
    Op("stl", "3D モデル (STL) で切り抜く", G_SHAPE, "STL ファイルの立体の中 (または外) の原子を消します (Atomsk -select stl)",
       [upload("stl", "STL ファイル", accept=".stl", hint="Blender・Tinkercad などで作った立体 (ASCII / バイナリ)"),
        choice("action", "操作", "keep", KEEP_REMOVE),
        choice("placement", "立体の置き方", "rescale", [
            {"value": "asis", "label": "ファイルの座標のまま (Å)"}, {"value": "center", "label": "セルの中央に置く"},
            {"value": "rescale", "label": "形を保ってセルに収まるよう拡大縮小"}, {"value": "fill", "label": "セルいっぱいに引き伸ばす"}])],
       b_stl),

    # 欠陥・界面
    Op("dislocation", "転位 (直線)", G_DEFECT, "等方弾性の変位場で直線転位を入れます (Atomsk -dislocation)",
       [choice("dtype", "種類", "screw", [{"value": "screw", "label": "らせん転位"}, {"value": "edge", "label": "刃状転位"},
                                         {"value": "edge_add", "label": "刃状 (原子面を挿入)"}, {"value": "edge_rm", "label": "刃状 (原子面を除去)"}]),
        axis_field("xi", "転位線の方向 ξ", "z"), axis_field("normal", "すべり面の法線 n", "y"),
        num("b", "バーガースベクトルの大きさ", 2.556, min=0.01, max=50, unit="Å", hint="fcc Cu の a/2<110> なら 2.556"),
        num("nu", "ポアソン比", 0.34, min=-1, max=0.5, show_if={"dtype": ["edge", "edge_add", "edge_rm"]}),
        frac("p1", "位置 1", 0.501, hint="転位線に垂直な面内の位置 (ξ=z なら x)"),
        frac("p2", "位置 2", 0.501, hint="(ξ=z なら y)")], b_dislocation),
    Op("loop", "転位ループ", G_DEFECT, "円形の転位ループを入れます (Atomsk -dislocation loop)",
       [frac("cx", "中心 x", 0.5), frac("cy", "中心 y", 0.5), frac("cz", "中心 z", 0.5),
        axis_field("normal", "ループ面の法線", "z"), num("radius", "半径", 10, min=0.5, max=1000, unit="Å"),
        num("bx", "b の x 成分", 0, min=-50, max=50, unit="Å"), num("by", "b の y 成分", 0, min=-50, max=50, unit="Å"),
        num("bz", "b の z 成分", 2.556, min=-50, max=50, unit="Å"), num("nu", "ポアソン比", 0.34, min=-1, max=0.5)],
       pipe(lambda p, o: p.dislocation_loop(box(o["cx"]), box(o["cy"]), box(o["cz"]), o["normal"], o["radius"],
                                            (o["bx"], o["by"], o["bz"]), o["nu"]))),
    Op("crack", "き裂", G_DEFECT, "線形破壊力学の変位場でき裂を入れます (Atomsk -crack)",
       [choice("mode", "モード", "I", [{"value": "I", "label": "I (開口)"}, {"value": "II", "label": "II (面内せん断)"},
                                      {"value": "III", "label": "III (面外せん断)"}]),
        choice("plane", "条件", "strain", [{"value": "strain", "label": "平面ひずみ"}, {"value": "stress", "label": "平面応力"}]),
        num("k", "応力拡大係数 K", 1.0, min=0.001, max=1000, unit="GPa·Å^½"),
        axis_field("xi", "き裂先端の線", "z"), axis_field("normal", "き裂面の法線", "y"),
        frac("p1", "先端の位置 1", 0.501, hint="原子にちょうど重ならない値に"), frac("p2", "先端の位置 2", 0.501),
        num("mu", "せん断弾性率 μ", 48, min=0.1, max=2000, unit="GPa"), num("nu", "ポアソン比", 0.34, min=-1, max=0.5)],
       pipe(lambda p, o: p.crack(o["mode"], o["plane"], o["k"], box(o["p1"]), box(o["p2"]), o["xi"], o["normal"],
                                 o["mu"], o["nu"]))),
    Op("bicrystal", "双結晶 (粒界)", G_DEFECT, "立方晶の単位胞を 2 つの方位に回して重ね、粒界を作ります (-orient と --merge)",
       [ptext("ax", "結晶 A の x", "[100]", MILLER), ptext("ay", "結晶 A の y", "[013]", MILLER), ptext("az", "結晶 A の z", "[0-31]", MILLER),
        ptext("bx", "結晶 B の x", "[100]", MILLER), ptext("by", "結晶 B の y", "[0-13]", MILLER), ptext("bz", "結晶 B の z", "[031]", MILLER),
        axis_field("stack", "重ねる方向 (粒界の法線)", "y"),
        num("lx", "大きさ x", 20, min=1, max=1000, unit="Å"), num("ly", "大きさ y (2 粒の合計)", 60, min=1, max=2000, unit="Å"),
        num("lz", "大きさ z", 20, min=1, max=1000, unit="Å"),
        num("rmd", "近すぎる原子の削除距離", 1.0, min=0, max=5, unit="Å", advanced=True, hint="0 で削除しない")],
       lambda c: ak.bicrystal(c.need_atoms(), (c.o["ax"], c.o["ay"], c.o["az"]), (c.o["bx"], c.o["by"], c.o["bz"]),
                              stack=c.o["stack"], size=(c.o["lx"], c.o["ly"], c.o["lz"]),
                              remove_doubles=c.o["rmd"] or None, log=c.warnings)),
    Op("place_molecule", "分子を置く", G_DEFECT,
       "登録した分子 (PubChem から取り込んだものなど) を、向きを回してから指定の位置に置き、この構造に加えます (吸着・溶媒和など)",
       [structure_ref("molecule", "置く分子"),
        num("x", "x の位置", None, min=-10000, max=10000, unit="Å", optional=True, hint="空欄ならセルの中央"),
        num("y", "y の位置", None, min=-10000, max=10000, unit="Å", optional=True, hint="空欄ならセルの中央"),
        num("z", "z の位置", None, min=-10000, max=10000, unit="Å", optional=True, hint="空欄ならセルの中央"),
        num("rx", "x 軸のまわりの回転", 0, min=-360, max=360, unit="°"),
        num("ry", "y 軸のまわりの回転", 0, min=-360, max=360, unit="°"),
        num("rz", "z 軸のまわりの回転", 0, min=-360, max=360, unit="°"),
        num("rmd", "分子と重なる原子の削除距離", 0.0, min=0, max=5, unit="Å", hint="0 で消さない")],
       b_place_molecule, uses_atomsk=False),
    Op("merge", "2 つの構造を重ねる", G_DEFECT, "別の構造を積み重ねて界面・積層構造を作るか、同じ箱に入れます (Atomsk --merge)",
       [structure_ref("other", "重ねる構造"),
        choice("mode", "重ね方", "stack", [{"value": "stack", "label": "ある方向に積む"}, {"value": "same", "label": "同じ箱に入れる"}]),
        axis_field("axis", "積む方向", "z", show_if={"mode": "stack"}),
        flag("scale", "面内の大きさを元の構造に合わせる", True, show_if={"mode": "stack"}),
        num("rmd", "近すぎる原子の削除距離", 0.0, min=0, max=5, unit="Å", hint="0 で削除しない")], b_merge),

    # 多結晶・組織
    Op("polycrystal", "多結晶 (Voronoi)", G_MICRO, "元の構造 (単位胞) から Voronoi 分割の多結晶を作ります (Atomsk --polycrystal)",
       [choice("dim", "粒の形", "3d", [{"value": "3d", "label": "3 次元 (等軸粒)"}, {"value": "2d", "label": "柱状 (z 方向にそろう)"}]),
        num("lx", "箱 x", 60, min=5, max=2000, unit="Å"), num("ly", "箱 y", 60, min=5, max=2000, unit="Å"),
        num("lz", "箱 z", 60, min=1, max=2000, unit="Å", show_if={"dim": "3d"}),
        num("nz", "z の繰り返し", 4, min=1, max=500, integer=True, show_if={"dim": "2d"}),
        num("grains", "粒の数", 8, min=1, max=500, integer=True),
        structure_ref("matrix", "粒界相 (粒のすき間を埋める構造)", optional=True, hint="選ばなければ粒界相なし"),
        num("clearance", "粒のすき間の半分", 0.1, min=0, max=50, unit="Å", hint="粒界相を入れるときは数 Å 以上に"),
        num("rmd", "近すぎる原子の削除距離", 1.0, min=0, max=5, unit="Å", advanced=True, hint="0 で削除しない"),
        seed_field()], b_polycrystal),
    Op("granular", "グラニュラー構造 (円柱粒)", G_MICRO,
       "円柱の結晶粒を並べ、すき間を原子で埋めた柱状組織を作ります。粒は Atomsk で円柱に切り出します",
       [choice("layout", "粒の並べ方", "random", [{"value": "random", "label": "ランダム"}, {"value": "hex", "label": "六方 (規則配列)"}]),
        num("lx", "箱 x", 150, min=5, max=2000, unit="Å"), num("ly", "箱 y", 150, min=5, max=2000, unit="Å"),
        num("lz", "厚さ z (円柱の軸方向)", 20, min=1, max=2000, unit="Å", hint="柱状なら結晶の周期の整数倍に丸めます"),
        num("grains", "粒の数", 10, min=1, max=500, integer=True, show_if={"layout": "random"}),
        num("radius", "粒の半径 (平均)", 18, min=1, max=1000, unit="Å"),
        num("spread", "半径のばらつき", 20, min=0, max=90, unit="%", show_if={"layout": "random"}, hint="±この割合で一様にばらつかせる"),
        num("gap", "粒どうしの最小のすき間", 4, min=0, max=200, unit="Å"),
        choice("orientation", "粒の方位", "columnar", [
            {"value": "columnar", "label": "z 軸まわりにランダム (柱状・z 方向は周期的)"},
            {"value": "random3d", "label": "3 次元でランダム (z 方向の端で格子が途切れる)"}]),
        choice("filler", "すき間", "random", [
            {"value": "random", "label": "原子をランダムに詰める (アモルファス状)"},
            {"value": "crystal", "label": "別の結晶で埋める"}, {"value": "none", "label": "空けておく"}]),
        choice("fill_species", "詰める元素", "auto", [{"value": "auto", "label": "粒と同じ組成"}, {"value": "custom", "label": "組成を指定"}],
               show_if={"filler": "random"}),
        ptext("fill_comp", "詰める組成", "Cu:100", COMPOSITION, show_if={"fill_species": "custom"}, hint="例: Cu:80,Zr:20"),
        num("fill_density", "充填率 (結晶の数密度に対して)", 90, min=1, max=110, unit="%", show_if={"filler": "random"}),
        structure_ref("matrix", "すき間を埋める結晶", show_if={"filler": "crystal"}),
        num("clearance", "粒の表面からあける距離", 1.5, min=0, max=20, unit="Å", advanced=True),
        num("dmin", "原子どうしの最小距離", None, min=0.3, max=10, unit="Å", optional=True, advanced=True,
            hint="空欄なら結晶の最近接距離の 0.75 倍"),
        seed_field()], b_granular),
]
OPS_BY_KEY = {op.key: op for op in OPS}
GROUPS = [G_EDIT, G_CREATE, G_CELL, G_SHAPE, G_DEFECT, G_MICRO]


def listing() -> dict:
    return {"groups": GROUPS, "ops": [op.listing() for op in OPS], "atomsk": ak.available()}


def get_op(key: str) -> Op:
    if key not in OPS_BY_KEY:
        raise ValueError("モデリングの種類が見つかりません")
    return OPS_BY_KEY[key]


def structure_refs(op: Op) -> list[str]:
    return [f["key"] for f in op.fields if f["type"] == "structure"]


def build(op: Op, atoms: Atoms | None, raw: dict, others: dict[str, Atoms]) -> tuple[Atoms, list[str], dict]:
    """操作を実行して (構造, 注意書き, 検証済みの入力) を返す。"""
    if op.needs_structure and atoms is None:
        raise ValueError("元の構造を選んでください")
    if op.uses_atomsk and not ak.available():
        raise ValueError(f"Atomsk が見つかりません ({ak.config.binary})")
    o = validate(op.fields, raw, atoms)
    ctx = Ctx(atoms=atoms, o=o, others=others)
    try:
        result = op.build(ctx)
    except ak.AtomskError as exc:
        raise ValueError(str(exc)) from None
    if len(result) > settings.MAX_ATOMS:
        raise ValueError(f"原子数が多すぎます ({len(result):,} > {settings.MAX_ATOMS:,})")
    return result, ctx.warnings, o


close_pairs = ak.close_pairs
CLOSE_FACTOR = 0.7   # 共有結合半径の和 × これより近い原子の組は「近すぎる」(MD で大きな力がかかり、構造が壊れる)


def too_close(atoms: Atoms, factor: float = CLOSE_FACTOR) -> str | None:
    """元素ごとの目安 (共有結合半径の和 × factor) より近い原子の組があれば、注意書きを返す。30 万原子を超える構造は調べない。"""
    if len(atoms) < 2 or len(atoms) > 300_000:
        return None
    import numpy as np
    from ase.data import covalent_radii
    from ase.neighborlist import neighbor_list
    radii = covalent_radii[atoms.numbers]
    i, j, d = neighbor_list("ijd", atoms, factor * 2 * float(radii.max()))
    keep = (i < j) & (d < factor * (radii[i] + radii[j]))
    if not keep.any():
        return None
    i, j, d = i[keep], j[keep], d[keep]
    k = int(np.argmin(d / (radii[i] + radii[j])))
    symbols = atoms.get_chemical_symbols()
    normal = radii[i[k]] + radii[j[k]]
    return (f"原子どうしが近すぎる組が {len(d)} 組あります (最も近いのは {symbols[i[k]]}-{symbols[j[k]]} が {d[k]:.2f} Å、"
            f"ふつうは {normal:.2f} Å 前後)。このまま MD をすると大きな力で構造が壊れます")


def _stack_scale_note(host: Atoms, other: Atoms, axis: str) -> str | None:
    """「ある方向に積む」で面内の大きさを合わせると、重ねる構造が伸び縮みする。その割合が大きければ注意する。"""
    import numpy as np
    plane = [k for k in range(3) if k != "xyz".index(axis)]
    ratios = [host.cell.lengths()[k] / other.cell.lengths()[k] for k in plane if other.cell.lengths()[k] > 0]
    worst = max((abs(r - 1) for r in ratios), default=0)
    if worst < 0.02:
        return None
    return (f"重ねる構造を面内で {' × '.join(f'{r:.2f}' for r in ratios)} 倍に伸び縮みさせました。分子や真空の箱に入れたものは"
            "形が崩れる (結合が縮む) ので、分子は「分子を置く」で置いてください")


def summary_text(op: Op, o: dict) -> str:
    """保存したときの「取得元」に残す短い説明。"""
    parts = []
    for spec in op.fields:
        if not _visible(spec, o) or spec["type"] in ("file", "structure") or spec["key"] == "seed":
            continue
        value = o.get(spec["key"])
        if value is None:
            continue
        if spec["type"] == "select":
            value = next((x["label"] for x in spec["options"] if x["value"] == value), value)
        elif spec["type"] == "bool":
            if not value:
                continue
            value = "はい"
        parts.append(f"{spec['label']}={value}")
    return f"{op.label}: " + ", ".join(parts)
