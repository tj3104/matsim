// 原子構造の 3D 表示。canvas に球を奥から順に描くだけの軽い実装で、指 1 本のドラッグで回転、
// ピンチ・ホイールで拡大縮小できる。フレームを渡せばトラジェクトリのアニメーションにもなる。
// 配位多面体 (setPolyhedra) は、面の三角形を原子と一緒に奥から順に描く (半透明)。
// 矢印 (setArrows、フォノンの振動の向きなど) も一緒に並べ、根元の原子より手前に描く。

// Jmol の配色 (よく使う元素)。ないものは灰色。
const COLORS = {
  H: "#e8e8e8", He: "#d9ffff", Li: "#cc80ff", Be: "#c2ff00", B: "#ffb5b5", C: "#707070", N: "#3050f8", O: "#ff0d0d",
  F: "#90e050", Ne: "#b3e3f5", Na: "#ab5cf2", Mg: "#8aff00", Al: "#bfa6a6", Si: "#f0c8a0", P: "#ff8000", S: "#e6c800",
  Cl: "#1ff01f", Ar: "#80d1e3", K: "#8f40d4", Ca: "#3dff00", Sc: "#c8c8c8", Ti: "#bfc2c7", V: "#a6a6ab", Cr: "#8a99c7",
  Mn: "#9c7ac7", Fe: "#e06633", Co: "#f090a0", Ni: "#50d050", Cu: "#c88033", Zn: "#7d80b0", Ga: "#c28f8f", Ge: "#668f8f",
  As: "#bd80e3", Se: "#ffa100", Br: "#a62929", Kr: "#5cb8d1", Rb: "#702eb0", Sr: "#00ff00", Y: "#94ffff", Zr: "#94e0e0",
  Nb: "#73c2c9", Mo: "#54b5b5", Ru: "#248f8f", Rh: "#0a7d8c", Pd: "#006985", Ag: "#c0c0c0", Cd: "#ffd98f", In: "#a67573",
  Sn: "#668080", Sb: "#9e63b5", Te: "#d47a00", I: "#940094", Xe: "#429eb0", Cs: "#57178f", Ba: "#00c900", La: "#70d4ff",
  Ce: "#ffffc7", Hf: "#4dc2ff", Ta: "#4da6ff", W: "#2194d6", Re: "#267dab", Os: "#266696", Ir: "#175487", Pt: "#d0d0e0",
  Au: "#ffd123", Hg: "#b8b8d0", Tl: "#a6544d", Pb: "#575961", Bi: "#9e4fb5",
};
// 共有結合半径 [Å] (表示用の目安)。ないものは 1.4。
const RADII = {
  H: 0.31, Li: 1.28, Be: 0.96, B: 0.84, C: 0.76, N: 0.71, O: 0.66, F: 0.57, Na: 1.66, Mg: 1.41, Al: 1.21, Si: 1.11,
  P: 1.07, S: 1.05, Cl: 1.02, K: 2.03, Ca: 1.76, Ti: 1.6, V: 1.53, Cr: 1.39, Mn: 1.39, Fe: 1.32, Co: 1.26, Ni: 1.24,
  Cu: 1.32, Zn: 1.22, Ga: 1.22, Ge: 1.2, As: 1.19, Se: 1.2, Br: 1.2, Sr: 1.95, Y: 1.9, Zr: 1.75, Nb: 1.64, Mo: 1.54,
  Pd: 1.39, Ag: 1.45, Sn: 1.39, Te: 1.38, I: 1.39, Ba: 2.15, La: 2.07, Hf: 1.75, Ta: 1.7, W: 1.62, Pt: 1.36, Au: 1.36,
  Pb: 1.46, Bi: 1.48,
};
const BOX_EDGES = [[0, 1], [0, 2], [0, 4], [1, 3], [1, 5], [2, 3], [2, 6], [3, 7], [4, 5], [4, 6], [5, 7], [6, 7]];

export const elementColor = (symbol) => COLORS[symbol] || "#b0b0b0";

function shade(hex, factor) {
  const n = parseInt(hex.slice(1), 16);
  const c = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => Math.round(v * factor));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

function boxCorners(cell) {
  const [a, b, c] = cell;
  const corners = [];
  for (let i = 0; i < 8; i += 1) {
    const f = [i & 1, (i >> 1) & 1, (i >> 2) & 1];
    corners.push([0, 1, 2].map((k) => f[0] * a[k] + f[1] * b[k] + f[2] * c[k]));
  }
  return corners;
}

export class Viewer {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.symbols = [];
    this.positions = [];
    this.cell = null;
    this.rotX = -1.15;   // 少し斜め上から見る
    this.rotZ = 0.5;
    this.zoom = 1;
    this.pointers = new Map();
    this.pinchDistance = 0;
    this.pending = false;
    this.polyhedra = null;
    this.arrows = null;
    this.bind();
    this.observer = new ResizeObserver(() => this.requestDraw());
    this.observer.observe(canvas);
  }

  destroy() {
    this.observer.disconnect();
  }

  setStructure({symbols, positions, cell}) {
    this.symbols = symbols;
    this.colors = symbols.map(elementColor);
    this.radii = symbols.map((s) => (RADII[s] || 1.4) * 0.45);
    this.hidden = null;
    this.setFrame(positions, cell, true);
  }

  // 配位多面体を描く。polyhedra は [{faces: [[[x,y,z] ×3], ...], color: "#rrggbb"}]。null で消す。
  setPolyhedra(polyhedra) {
    this.polyhedra = polyhedra && polyhedra.length ? polyhedra : null;
    this.requestDraw();
  }

  // 矢印を描く。arrows は [{from: [x,y,z], to: [x,y,z], color: "#rrggbb"}]。null で消す。
  setArrows(arrows) {
    this.arrows = arrows && arrows.length ? arrows : null;
    this.requestDraw();
  }

  // 原子ごとの色を指定する (解析結果の色塗り)。colors[i] が null の原子は灰色で小さく描くか、hideOthers なら描かない。
  // colors を null にすると元素の色に戻す。
  setColors(colors, {hideOthers = false} = {}) {
    const base = this.symbols.map((s) => (RADII[s] || 1.4) * 0.45);
    if (!colors) {
      this.colors = this.symbols.map(elementColor);
      this.radii = base;
      this.hidden = null;
    } else {
      this.colors = colors.map((c) => c || "#c4c8d0");
      this.radii = base.map((r, i) => (colors[i] ? r : r * 0.55));
      this.hidden = hideOthers ? colors.map((c) => !c) : null;
    }
    this.requestDraw();
  }

  // refit を付けると表示範囲を計算し直す (アニメーション中は付けない: セルの揺れで全体が動いて見えるため)
  setFrame(positions, cell, refit = false) {
    this.positions = positions;
    this.cell = cell;
    if (refit) {
      const corners = boxCorners(cell);
      this.center = [0, 1, 2].map((k) => corners.reduce((sum, p) => sum + p[k], 0) / 8);
      this.extent = Math.max(...corners.map((p) => Math.hypot(p[0] - this.center[0], p[1] - this.center[1], p[2] - this.center[2]))) || 1;
    }
    this.requestDraw();
  }

  bind() {
    const canvas = this.canvas;
    canvas.addEventListener("pointerdown", (event) => {
      canvas.setPointerCapture(event.pointerId);
      this.pointers.set(event.pointerId, [event.clientX, event.clientY]);
      this.pinchDistance = this.currentPinch();
    });
    canvas.addEventListener("pointermove", (event) => {
      const last = this.pointers.get(event.pointerId);
      if (!last) return;
      this.pointers.set(event.pointerId, [event.clientX, event.clientY]);
      if (this.pointers.size === 1) {
        this.rotZ += (event.clientX - last[0]) * 0.01;
        this.rotX = Math.max(-Math.PI, Math.min(0, this.rotX + (event.clientY - last[1]) * 0.01));
      } else {
        const distance = this.currentPinch();
        if (this.pinchDistance > 0) this.setZoom(this.zoom * distance / this.pinchDistance);
        this.pinchDistance = distance;
      }
      this.requestDraw();
    });
    const release = (event) => {
      this.pointers.delete(event.pointerId);
      this.pinchDistance = this.currentPinch();
    };
    canvas.addEventListener("pointerup", release);
    canvas.addEventListener("pointercancel", release);
    canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      this.setZoom(this.zoom * (event.deltaY < 0 ? 1.1 : 0.9));
      this.requestDraw();
    }, {passive: false});
  }

  currentPinch() {
    if (this.pointers.size < 2) return 0;
    const [p, q] = [...this.pointers.values()];
    return Math.hypot(p[0] - q[0], p[1] - q[1]);
  }

  setZoom(value) {
    this.zoom = Math.max(0.3, Math.min(8, value));
  }

  requestDraw() {
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => {
      this.pending = false;
      this.draw();
    });
  }

  draw() {
    const {canvas, ctx} = this;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height || !this.cell) return;
    const ratio = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const scale = Math.min(width, height) / (2.1 * this.extent) * this.zoom;
    const [cz, sz, cx, sx] = [Math.cos(this.rotZ), Math.sin(this.rotZ), Math.cos(this.rotX), Math.sin(this.rotX)];
    const [ox, oy, oz] = this.center;
    // z 軸まわり → x 軸まわりに回して、画面の x, y と奥行きにする
    const project = (p) => {
      const x = p[0] - ox, y = p[1] - oy, z = p[2] - oz;
      const x1 = x * cz - y * sz;
      const y1 = x * sz + y * cz;
      return [width / 2 + x1 * scale, height / 2 + (y1 * cx - z * sx) * scale, y1 * sx + z * cx];
    };

    const style = getComputedStyle(canvas);
    ctx.strokeStyle = style.getPropertyValue("--muted").trim() || "#888";
    ctx.lineWidth = 1;
    ctx.beginPath();
    const corners = boxCorners(this.cell).map(project);
    for (const [i, j] of BOX_EDGES) {
      ctx.moveTo(corners[i][0], corners[i][1]);
      ctx.lineTo(corners[j][0], corners[j][1]);
    }
    ctx.stroke();

    const n = Math.min(this.positions.length, this.symbols.length);
    const projected = [];
    for (let i = 0; i < n; i += 1) {
      const p = project(this.positions[i]);
      projected.push([p[0], p[1], p[2], i, null]);
    }
    // 多面体の面: 3 頂点を投影し、奥行きは重心で並べる
    if (this.polyhedra) {
      for (const poly of this.polyhedra) {
        for (const face of poly.faces) {
          const pts = face.map(project);
          projected.push([0, 0, (pts[0][2] + pts[1][2] + pts[2][2]) / 3, -1, {pts, color: poly.color}]);
        }
      }
    }
    if (this.arrows) {
      for (const arrow of this.arrows) {
        const a = project(arrow.from);
        const b = project(arrow.to);
        // 根元の原子より手前 (奥行きを少し大きく) に描いて、原子に隠れないようにする
        projected.push([0, 0, Math.max(a[2], b[2]) + 0.02 * this.extent, -2, {arrow: [a, b], color: arrow.color}]);
      }
    }
    projected.sort((a, b) => a[2] - b[2]);
    const outline = n <= 3000;   // 多いときは輪郭線を省いて軽くする
    for (const [x, y, depth, i, face] of projected) {
      if (face && face.arrow) {
        const [a, b] = face.arrow;
        const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
        if (length < 1) continue;
        const ux = (b[0] - a[0]) / length;
        const uy = (b[1] - a[1]) / length;
        const head = Math.min(9, 0.45 * length);
        ctx.strokeStyle = face.color;
        ctx.fillStyle = face.color;
        ctx.lineWidth = 2.8;
        ctx.beginPath();
        ctx.moveTo(a[0], a[1]);
        ctx.lineTo(b[0] - ux * head * 0.8, b[1] - uy * head * 0.8);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(b[0], b[1]);
        ctx.lineTo(b[0] - ux * head - uy * head * 0.5, b[1] - uy * head + ux * head * 0.5);
        ctx.lineTo(b[0] - ux * head + uy * head * 0.5, b[1] - uy * head - ux * head * 0.5);
        ctx.closePath();
        ctx.fill();
        continue;
      }
      if (face) {
        ctx.beginPath();
        ctx.moveTo(face.pts[0][0], face.pts[0][1]);
        ctx.lineTo(face.pts[1][0], face.pts[1][1]);
        ctx.lineTo(face.pts[2][0], face.pts[2][1]);
        ctx.closePath();
        ctx.globalAlpha = 0.32;
        ctx.fillStyle = face.color;
        ctx.fill();
        ctx.globalAlpha = 0.85;
        ctx.lineWidth = 0.8;
        ctx.strokeStyle = face.color;
        ctx.stroke();
        ctx.globalAlpha = 1;
        continue;
      }
      if (this.hidden && this.hidden[i]) continue;
      const radius = Math.max(1, this.radii[i] * scale);
      // 奥の原子を少し暗くして前後関係を分かりやすくする
      ctx.fillStyle = shade(this.colors[i], 0.72 + 0.28 * (depth / this.extent + 1) / 2);
      ctx.lineWidth = 0.6;
      ctx.strokeStyle = "rgba(0,0,0,.45)";
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.fill();
      if (outline) ctx.stroke();
    }
  }
}

// 元素と色の凡例
export function legendItems(symbols) {
  const counts = new Map();
  for (const symbol of symbols) counts.set(symbol, (counts.get(symbol) || 0) + 1);
  return [...counts.entries()].map(([symbol, count]) => ({symbol, count, color: elementColor(symbol)}));
}
