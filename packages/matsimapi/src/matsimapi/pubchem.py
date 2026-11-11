"""PubChem から有機分子を取り込む。名前・CID・SMILES で探し、3D の配座 (SDF) を取って、全方位に真空層を付けた箱に入れる。

箱に入れるのは、MD (GPUMD・SevenNet) も第一原理計算 (ezcal・QE) も周期境界の箱を前提にするため。分子どうしが周期像と
相互作用しないよう、既定で各方向に 10 Å の真空を空ける。
"""
from __future__ import annotations

import io
import json
import re
import urllib.parse
import urllib.request

from ase import Atoms
from ase.io import read as ase_read

BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound"
TIMEOUT = 20
MAX_RESULTS = 10
DEFAULT_VACUUM = 10.0


def _get(url: str, form: dict | None = None) -> bytes:
    data = urllib.parse.urlencode(form).encode() if form else None
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "matsimapi"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):   # 400 は名前・SMILES として読めなかったとき
            raise ValueError("PubChem に見つかりませんでした") from None
        raise ValueError(f"PubChem に問い合わせられませんでした ({exc.code})") from None
    except urllib.error.URLError as exc:
        raise ValueError(f"PubChem につながりません ({exc.reason})") from None


def _candidates(query: str) -> list[tuple[str, str]]:
    """問い合わせ方の順番: 数字だけなら CID。SMILES にしかない記号があれば SMILES。それ以外は名前で探し、なければ SMILES
    (CCO のように名前とも読める SMILES のため。名前で引くと別の化合物に当たることがあるので、元素記号だけの並びは SMILES を先に)。"""
    query = query.strip()
    if not query or len(query) > 300:
        raise ValueError("分子の名前・CID・SMILES を 1〜300 文字で入れてください")
    if query.isdigit():
        return [("cid", query)]
    if re.search(r"[=#\[\]()@/\\0-9]", query) and " " not in query:
        return [("smiles", query), ("name", query)]
    if re.fullmatch(r"(?:Cl|Br|[BCNOPSFI]|[cnos])+", query):
        return [("smiles", query), ("name", query)]
    return [("name", query), ("smiles", query)]


def search(query: str) -> list[dict]:
    """候補 (CID・名前・組成式・分子量) を返す。"""
    last: ValueError | None = None
    for space, value in _candidates(query):
        properties = "property/Title,MolecularFormula,MolecularWeight,IUPACName/JSON"
        rows = []
        # SMILES は記号 (/ や #) を URL に入れると読み違えるので POST で渡す。PubChem は正しい SMILES でもときどき
        # 「読めない」と返すので、SMILES はもう一度だけ試す
        for _ in range(2 if space == "smiles" else 1):
            try:
                raw = (_get(f"{BASE}/smiles/{properties}", {"smiles": value}) if space == "smiles"
                       else _get(f"{BASE}/{space}/{urllib.parse.quote(value, safe='')}/{properties}"))
                rows = json.loads(raw)["PropertyTable"]["Properties"][:MAX_RESULTS]
                break
            except ValueError as exc:
                last = exc
        rows = [row for row in rows if row.get("CID")]   # SMILES で未登録の分子は CID 0 で返る
        if rows:
            return [{"cid": row["CID"], "name": row.get("Title") or row.get("IUPACName") or f"CID {row['CID']}",
                     "iupac": row.get("IUPACName"), "formula": row.get("MolecularFormula"),
                     "weight": float(row["MolecularWeight"]) if row.get("MolecularWeight") else None} for row in rows]
    raise last or ValueError("PubChem に見つかりませんでした")


def boxed(atoms: Atoms, vacuum: float) -> Atoms:
    """分子 (や集まり) を、全方位に vacuum Å の真空を空けた直方体の箱の中央に置く。"""
    atoms = atoms.copy()
    atoms.set_cell([0, 0, 0])
    atoms.pbc = False
    atoms.center(vacuum=vacuum)
    atoms.pbc = True
    return atoms


def fetch(cid: int, vacuum: float = DEFAULT_VACUUM) -> tuple[Atoms, dict]:
    """3D の配座を取って箱に入れる。3D のない分子 (大きすぎる・塩など) は断る。"""
    if not 0 < vacuum <= 100:
        raise ValueError("真空層は 0〜100 Å にしてください")
    info = search(str(int(cid)))[0]
    try:
        sdf = _get(f"{BASE}/cid/{int(cid)}/SDF?record_type=3d").decode()
    except ValueError as exc:
        raise ValueError(f"{info['name']} には PubChem の 3D 配座がありません ({exc})") from None
    atoms = ase_read(io.StringIO(sdf), format="sdf")
    return boxed(atoms, vacuum), info
