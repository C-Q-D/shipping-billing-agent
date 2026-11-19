/**
 * Lumno Background & Texture Engine (Zero-Dependency Vanilla JS)
 *
 * Exports 4 production utilities to create authentic Lumno-style backgrounds & card textures
 * WITHOUT relying on Lumno's original wallpaper image files:
 *
 * 1. `mountLitCardDither(container)`
 *    Renders the 2x2px 4x4 Bayer top-lit paper grain used inside `.home-lit` Bento cards.
 *
 * 2. `mountImageBayerDither(canvas, sourceImage, options)`
 *    Applies Lumno's exact 2x2px 4x4 Bayer 8-level RGB ordered dither to an already high-key image.
 *
 * 3. `lumnoizeImageToCanvas(canvas, sourceImage, options)`
 *    Transforms ANY external landscape photo or CC0 museum painting into Lumno's signature
 *    high-key chalky pastel palette (lifts dark shadows, mutes saturation, optionally feathers
 *    the upper 58% into warm ivory UI-safe negative space) and applies Bayer dither or oil grain.
 *
 * 4. `mountProceduralLumnoBackground(container, options)`
 *    Zero-asset procedural Impressionist brushwork + Bayer dither / oil-grain painter.
 *    Generates brand-new backgrounds across 5 Lumno genres (`meadow`, `snow-village`,
 *    `coast`, `shanshui`, `nocturne`) for any integer `seed`.
 */

const BAYER_4X4 = [
  0,  8,  2, 10,
  12, 4, 14,  6,
  3, 11,  1,  9,
  15, 7, 13,  5,
];

function createSeededRandom(seed = 42) {
  let s = (Number(seed) || 42) >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function clampByte(v) {
  return v < 0 ? 0 : v > 255 ? 255 : Math.round(v);
}

function buildBayerLut({ brightness = 0.01, contrast = 1.08, grain = 0.78, levels = 8 } = {}) {
  const lut = new Uint8ClampedArray(16 * 256);
  const step = 255 / Math.max(1, levels - 1);
  for (let b = 0; b < 16; b++) {
    const offset = (b * 3 - 24) * grain;
    for (let v = 0; v < 256; v++) {
      const adj = Math.min(255, Math.max(0, (v - 128) * contrast + 128 + brightness * 255));
      const dithered = Math.min(255, Math.max(0, adj + offset));
      lut[b * 256 + v] = Math.round(dithered / step) * step;
    }
  }
  return lut;
}

function applyBayerLutToImageData(imgData, lut) {
  const { width, height, data } = imgData;
  for (let y = 0; y < height; y++) {
    const rowBayer = (y & 3) * 4;
    let idx = y * width * 4;
    for (let x = 0; x < width; x++, idx += 4) {
      const bOffset = BAYER_4X4[rowBayer + (x & 3)] * 256;
      data[idx]     = lut[bOffset + data[idx]];
      data[idx + 1] = lut[bOffset + data[idx + 1]];
      data[idx + 2] = lut[bOffset + data[idx + 2]];
      data[idx + 3] = 255;
    }
  }
}

function applyCanvasGrain(imgData, amount = 0.14, seed = 7) {
  const { width, height, data } = imgData;
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      let h = (Math.imul(x, 374761393) ^ Math.imul(y, 668265263) ^ seed) >>> 0;
      h = Math.imul(h ^ (h >>> 13), 1274126177) >>> 0;
      const n = (((h ^ (h >>> 16)) & 255) - 127.5) * amount;
      const idx = (y * width + x) * 4;
      data[idx]     = clampByte(data[idx] + n);
      data[idx + 1] = clampByte(data[idx + 1] + n);
      data[idx + 2] = clampByte(data[idx + 2] + n);
    }
  }
}

/* 1. Bento / Open-Source Lit Card 2x2px Bayer Paper Grain */
export function mountLitCardDither(container) {
  const canvas = document.createElement("canvas");
  canvas.style.cssText = "display:block;width:100%;height:100%;image-rendering:pixelated;";
  container.appendChild(canvas);
  const ctx = canvas.getContext("2d", { alpha: false });

  const render = () => {
    const rect = container.getBoundingClientRect();
    const cols = Math.max(1, Math.ceil(rect.width / 2));
    const rows = Math.max(1, Math.ceil(rect.height / 2));
    if (canvas.width === cols && canvas.height === rows) return;
    canvas.width = cols;
    canvas.height = rows;

    const imgData = ctx.createImageData(cols, rows);
    const data = imgData.data;

    for (let y = 0; y < rows; y++) {
      const ty = rows > 1 ? y / (rows - 1) : 0;
      const baseR = 244 + (236 - 244) * ty;
      const baseG = 245 + (238 - 245) * ty;
      const baseB = 247 + (241 - 247) * ty;

      for (let x = 0; x < cols; x++) {
        const tx = cols > 1 ? x / (cols - 1) : 0.5;
        const dx = (tx - 0.5) / 1.2;
        const dy = ty / 0.7;
        const dist = Math.sqrt(dx * dx + dy * dy);
        const glow = dist < 0.62 ? Math.pow(1 - dist / 0.62, 1.6) * 0.85 : 0;

        const r = baseR + (255 - baseR) * glow;
        const g = baseG + (255 - baseG) * glow;
        const b = baseB + (255 - baseB) * glow;

        const luma = 0.2126 * r + 0.7152 * g + 0.0722 * b;
        const bayer = BAYER_4X4[(y & 3) * 4 + (x & 3)];
        const threshold = ((bayer + 0.5) / 16 - 0.5) * 10;
        const delta = Math.round((luma + threshold) / 10) * 10 - luma;

        const idx = (y * cols + x) * 4;
        data[idx]     = clampByte(r + delta);
        data[idx + 1] = clampByte(g + delta);
        data[idx + 2] = clampByte(b + delta);
        data[idx + 3] = 255;
      }
    }
    ctx.putImageData(imgData, 0, 0);
  };

  render();
  const ro = new ResizeObserver(render);
  ro.observe(container);
  return () => ro.disconnect();
}

/* 2. Direct 2x2px Bayer 8-Level Dither for High-Key Art */
export function mountImageBayerDither(canvas, sourceImage, options = {}) {
  const cellSize = Math.max(1, Math.floor(options.cellSize ?? 2));
  const cols = Math.max(1, Math.ceil((canvas.clientWidth || 800) / cellSize));
  const rows = Math.max(1, Math.ceil((canvas.clientHeight || 450) / cellSize));
  canvas.width = cols;
  canvas.height = rows;
  canvas.style.imageRendering = "pixelated";

  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const scale = Math.max(cols / sourceImage.naturalWidth, rows / sourceImage.naturalHeight);
  const dw = Math.ceil(sourceImage.naturalWidth * scale);
  const dh = Math.ceil(sourceImage.naturalHeight * scale);
  ctx.drawImage(sourceImage, Math.floor((cols - dw) / 2), Math.floor((rows - dh) / 2), dw, dh);

  const imgData = ctx.getImageData(0, 0, cols, rows);
  applyBayerLutToImageData(imgData, buildBayerLut(options));
  ctx.putImageData(imgData, 0, 0);
}

/* 3. Convert ANY External Photo / Museum Painting into Lumno Chalky Pastel + Dither/Oil */
export function lumnoizeImageToCanvas(canvas, sourceImage, options = {}) {
  const {
    mode = "dither",          // "dither" (Image 1/3 style) | "oil-veil" (Image 2 style)
    cellSize = mode === "dither" ? 2 : 1,
    saturationScale = 0.66,   // Mutes harsh photographic colors into chalky pigment
    shadowLift = mode === "dither" ? 0.64 : 0.22, // Lifts dark blacks toward warm ivory
    ivoryTint = [247, 248, 239],
    topSkyFade = mode === "dither", // Feathers upper 58% into breathable UI-safe sky
  } = options;

  const cols = Math.max(1, Math.ceil((canvas.clientWidth || 960) / cellSize));
  const rows = Math.max(1, Math.ceil((canvas.clientHeight || 540) / cellSize));
  canvas.width = cols;
  canvas.height = rows;
  canvas.style.imageRendering = mode === "dither" ? "pixelated" : "auto";

  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const scale = Math.max(cols / sourceImage.naturalWidth, rows / sourceImage.naturalHeight);
  const dw = Math.ceil(sourceImage.naturalWidth * scale);
  const dh = Math.ceil(sourceImage.naturalHeight * scale);
  ctx.drawImage(sourceImage, Math.floor((cols - dw) / 2), Math.floor((rows - dh) / 2), dw, dh);

  const imgData = ctx.getImageData(0, 0, cols, rows);
  const data = imgData.data;

  for (let y = 0; y < rows; y++) {
    const ty = rows > 1 ? y / (rows - 1) : 0;
    let skyVeil = 0;
    if (topSkyFade && ty < 0.62) {
      const u = 1 - ty / 0.62;
      skyVeil = u * u * (3 - 2 * u) * 0.82;
    } else if (mode === "oil-veil") {
      // 3-stop concave atmospheric veil: 42% top -> 14% at 0.42 -> 28% bottom
      skyVeil = ty < 0.42
        ? 0.42 + (0.14 - 0.42) * (ty / 0.42)
        : 0.14 + (0.28 - 0.14) * ((ty - 0.42) / 0.58);
    }

    for (let x = 0; x < cols; x++) {
      const idx = (y * cols + x) * 4;
      let r = data[idx], g = data[idx + 1], b = data[idx + 2];

      // Step A: Desaturate toward luminance
      const luma = 0.299 * r + 0.587 * g + 0.114 * b;
      r = luma + (r - luma) * saturationScale;
      g = luma + (g - luma) * saturationScale;
      b = luma + (b - luma) * saturationScale;

      // Step B: Chalky shadow lift toward warm ivory
      const lift = shadowLift * (1 - (luma / 255) * 0.65);
      r = r + (ivoryTint[0] - r) * lift;
      g = g + (ivoryTint[1] - g) * lift;
      b = b + (ivoryTint[2] - b) * lift;

      // Step C: Top UI-safe sky feather or concave atmospheric veil
      r = r + (ivoryTint[0] - r) * skyVeil;
      g = g + (ivoryTint[1] - g) * skyVeil;
      b = b + (ivoryTint[2] - b) * skyVeil;

      data[idx]     = clampByte(r);
      data[idx + 1] = clampByte(g);
      data[idx + 2] = clampByte(b);
    }
  }

  if (mode === "dither") {
    applyBayerLutToImageData(imgData, buildBayerLut({ brightness: 0.01, contrast: 1.08, grain: 0.78 }));
  } else {
    applyCanvasGrain(imgData, 0.14);
  }
  ctx.putImageData(imgData, 0, 0);
}

/* 4. Zero-Asset Procedural Impressionist Painter + Bayer / Oil Engine */
function paintProceduralScene(ctx, w, h, preset = "meadow", seed = 101) {
  const rand = createSeededRandom(seed);

  const daub = (x, y, rx, ry, angle, color, alpha = 0.45) => {
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(angle);
    ctx.globalAlpha = alpha;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.ellipse(0, 0, Math.max(1, rx), Math.max(1, ry), 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  };

  const ridge = (baseY, amp, freq, phase, color, alpha = 1, cradleSide = 0) => {
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(0, h);
    for (let x = 0; x <= w; x += Math.max(2, w / 120)) {
      const nx = x / w;
      const cradle = cradleSide === 0
        ? Math.pow(Math.abs(nx - 0.48) * 1.9, 2.1) * h * 0.09
        : cradleSide > 0
          ? Math.pow(nx, 2.2) * h * 0.18
          : Math.pow(1 - nx, 2.2) * h * 0.18;
      const wave = Math.sin(nx * freq + phase) * amp + Math.cos(nx * freq * 2.3 + phase * 1.7) * (amp * 0.42);
      ctx.lineTo(x, baseY - cradle + wave);
    }
    ctx.lineTo(w, h);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  };

  if (preset === "meadow") {
    // High-key Monet pastoral meadow: top 64% warm ivory sky, low U-cradle hills & wildflowers
    const sky = ctx.createLinearGradient(0, 0, 0, h);
    sky.addColorStop(0, "#f8f9f1");
    sky.addColorStop(0.56, "#f4f6ec");
    sky.addColorStop(0.76, "#e8eee7");
    sky.addColorStop(1, "#dce6d5");
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);

    // Subtle warm cream & pale blue impasto sky strokes
    for (let i = 0; i < 180; i++) {
      const x = rand() * w, y = rand() * h * 0.65;
      const cols = ["#fbfcf6", "#f2f5eb", "#edf2f5", "#f7f5e8"];
      daub(x, y, w * (0.03 + rand() * 0.06), h * (0.012 + rand() * 0.02), (rand() - 0.5) * 0.3, cols[i % 4], 0.36);
    }

    // Distant lavender-blue hills & sage rolling meadows
    ridge(h * 0.72, h * 0.022, 5.5, rand() * 6, "#d7e0eb", 0.85, 1);
    ridge(h * 0.75, h * 0.018, 7.2, rand() * 6, "#dce5dd", 0.9, -1);
    ridge(h * 0.80, h * 0.025, 4.8, rand() * 6, "#d7e3c8", 0.95, 0);

    // Distant warm stone cottage nestled on right hillside
    const cx = w * (0.76 + rand() * 0.06), cy = h * 0.71;
    daub(cx - w * 0.025, cy - h * 0.02, w * 0.012, h * 0.048, -0.05, "#a9bca8", 0.85);
    daub(cx - w * 0.012, cy - h * 0.015, w * 0.01, h * 0.038, 0.04, "#b8c8b4", 0.8);
    ctx.fillStyle = "#ece7da";
    ctx.fillRect(cx, cy, w * 0.042, h * 0.032);
    ctx.fillStyle = "#dfc3a8";
    ctx.beginPath();
    ctx.moveTo(cx - w * 0.004, cy);
    ctx.lineTo(cx + w * 0.021, cy - h * 0.022);
    ctx.lineTo(cx + w * 0.046, cy);
    ctx.closePath();
    ctx.fill();

    // Textured impasto meadow grass & wildflower specks (concentrated bottom + left cradle)
    const grassPalette = ["#c8d8b4", "#bfd1ae", "#d9e3be", "#b2c6a6", "#e6e8c8", "#cddac2"];
    for (let i = 0; i < 650; i++) {
      const nx = rand();
      const topLimit = 0.77 - Math.pow(Math.max(0, 0.55 - nx), 1.8) * 0.22 - Math.pow(Math.max(0, nx - 0.6), 2) * 0.14;
      const ny = topLimit + rand() * (1.02 - topLimit);
      daub(nx * w, ny * h, w * (0.006 + rand() * 0.018), h * (0.004 + rand() * 0.011), (rand() - 0.5) * 0.7, grassPalette[i % grassPalette.length], 0.55);
    }
    const flowerPalette = ["#fbf9ec", "#f5e7a6", "#e9c6cf", "#bfd5ee", "#ffffff"];
    for (let i = 0; i < 260; i++) {
      const nx = Math.pow(rand(), 1.35) * 0.96;
      const ny = 0.74 + rand() * 0.25 - (nx < 0.35 ? (0.35 - nx) * 0.22 : 0);
      daub(nx * w, ny * h, w * (0.002 + rand() * 0.005), h * (0.003 + rand() * 0.006), rand() * 3, flowerPalette[i % flowerPalette.length], 0.78);
    }
  } else if (preset === "snow-village") {
    // Pissarro / Sisley overcast winter village oil (User Image 2 paradigm)
    const sky = ctx.createLinearGradient(0, 0, 0, h);
    sky.addColorStop(0, "#d8dad6");
    sky.addColorStop(0.35, "#cfd2ce");
    sky.addColorStop(1, "#deddd7");
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);

    ridge(h * 0.42, h * 0.04, 3.8, rand() * 5, "#b7bab6", 0.95, -1);
    ridge(h * 0.56, h * 0.03, 5.2, rand() * 5, "#c8cac5", 0.92, 1);

    // Impasto hillside brushstrokes
    const snowTones = ["#e4e5e0", "#d3d5d0", "#bfc2be", "#aeb2ae", "#cbc6bc", "#dcded9"];
    for (let i = 0; i < 900; i++) {
      daub(rand() * w, rand() * h, w * (0.008 + rand() * 0.025), h * (0.005 + rand() * 0.014), (rand() - 0.5) * 0.5, snowTones[i % snowTones.length], 0.42);
    }

    // Muted ochre cottages with chalky snow-laden gabled roofs on left & right edges
    const houses = [
      { x: 0.04, y: 0.52, bw: 0.15, bh: 0.30, wall: "#b8a38c", roof: "#eaebe6" },
      { x: 0.18, y: 0.56, bw: 0.16, bh: 0.25, wall: "#c2ab93", roof: "#e5e6e1" },
      { x: 0.74, y: 0.38, bw: 0.24, bh: 0.48, wall: "#bba48d", roof: "#ecece7" },
    ];
    for (const hs of houses) {
      const hx = hs.x * w, hy = hs.y * h, hw = hs.bw * w, hh = hs.bh * h;
      ctx.fillStyle = hs.wall;
      ctx.fillRect(hx, hy, hw, hh);
      for (let k = 0; k < 45; k++) {
        daub(hx + rand() * hw, hy + rand() * hh, w * 0.012, h * 0.008, (rand() - 0.5) * 0.4, k % 2 ? "#cbb59d" : "#9e8c79", 0.45);
      }
      ctx.fillStyle = hs.roof;
      ctx.beginPath();
      ctx.moveTo(hx - w * 0.01, hy);
      ctx.lineTo(hx + hw * 0.45, hy - hh * 0.46);
      ctx.lineTo(hx + hw + w * 0.01, hy - hh * 0.08);
      ctx.lineTo(hx + hw, hy);
      ctx.closePath();
      ctx.fill();
      // Muted slate windows
      ctx.fillStyle = "#8a9290";
      for (let wx = 0.2; wx < 0.8; wx += 0.32) {
        ctx.fillRect(hx + hw * wx, hy + hh * 0.22, hw * 0.14, hh * 0.2);
        ctx.fillRect(hx + hw * wx, hy + hh * 0.56, hw * 0.14, hh * 0.22);
      }
    }
  } else if (preset === "coast") {
    // High-key Seurat / Monet coastal cliff & shimmering water
    const sky = ctx.createLinearGradient(0, 0, 0, h);
    sky.addColorStop(0, "#f7f8f3");
    sky.addColorStop(0.62, "#f3f6f4");
    sky.addColorStop(0.66, "#dfecee");
    sky.addColorStop(1, "#d2e4e6");
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);

    // Shimmering pointillist sea ripples
    const seaCols = ["#cbe0e5", "#d8eaec", "#f2f6f2", "#bfd7e2", "#d9e7dd"];
    for (let i = 0; i < 620; i++) {
      const nx = rand(), ny = 0.65 + rand() * 0.35;
      daub(nx * w, ny * h, w * (0.006 + rand() * 0.018), h * 0.004, 0, seaCols[i % seaCols.length], 0.6);
    }
    // Right coastal chalk cliff & warm pastel meadow top
    ridge(h * 0.82, h * 0.015, 4.2, 1.2, "#e8e3d6", 0.96, 1);
    const cliffCols = ["#d6dce0", "#e9decb", "#c5d4c0", "#dfcbbd", "#b8c9b8"];
    for (let i = 0; i < 380; i++) {
      const nx = 0.58 + rand() * 0.42;
      const ny = 0.66 + (1 - (nx - 0.58) / 0.42) * 0.16 + rand() * 0.22;
      daub(nx * w, ny * h, w * 0.01, h * 0.009, rand() * 2, cliffCols[i % cliffCols.length], 0.62);
    }
  } else if (preset === "shanshui") {
    // Minimalist Song/Ming ink & mineral wash on warm Xuan paper
    ctx.fillStyle = "#f2f0e8";
    ctx.fillRect(0, 0, w, h);
    for (let i = 0; i < 240; i++) {
      daub(rand() * w, rand() * h, w * 0.04, h * 0.006, (rand() - 0.5) * 0.2, "#eae6db", 0.35);
    }
    ridge(h * 0.76, h * 0.05, 6.2, rand() * 5, "#dfe0d8", 0.7, 1);
    ridge(h * 0.80, h * 0.045, 7.8, rand() * 5, "#cfd3ca", 0.78, -1);
    ridge(h * 0.86, h * 0.03, 5.0, rand() * 5, "#bec4ba", 0.88, -1);
    for (let i = 0; i < 260; i++) {
      const nx = Math.pow(rand(), 1.7) * 0.45;
      const ny = 0.58 + nx * 0.45 + rand() * 0.22;
      daub(nx * w, ny * h, w * 0.008, h * 0.012, (rand() - 0.5), i % 5 === 0 ? "#c8b99e" : "#9aa396", 0.42);
    }
  } else {
    // Dark mode Lily Nocturne (#151b22 -> #1f2a33)
    const sky = ctx.createLinearGradient(0, 0, 0, h);
    sky.addColorStop(0, "#161c23");
    sky.addColorStop(0.65, "#1b232b");
    sky.addColorStop(1, "#202b2d");
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);
    // Soft misty moon in upper-left
    daub(w * 0.2, h * 0.22, w * 0.045, h * 0.06, 0, "#33404c", 0.35);
    daub(w * 0.2, h * 0.22, w * 0.011, h * 0.018, 0, "#cbd2c6", 0.65);
    const pads = ["#263532", "#2d3d37", "#343a46", "#232f36"];
    for (let i = 0; i < 340; i++) {
      daub(rand() * w, h * (0.72 + rand() * 0.28), w * (0.012 + rand() * 0.03), h * 0.006, 0, pads[i % 4], 0.6);
    }
    for (let i = 0; i < 18; i++) {
      daub(w * (0.15 + rand() * 0.75), h * (0.78 + rand() * 0.18), w * 0.008, h * 0.006, 0, "#9e9098", 0.65);
    }
  }
}

export function mountProceduralLumnoBackground(container, options = {}) {
  const {
    preset = "meadow", // "meadow" | "snow-village" | "coast" | "shanshui" | "nocturne"
    mode = preset === "snow-village" ? "oil-veil" : "dither", // "dither" | "oil-veil"
    seed = 108,
  } = options;

  let canvas = container.querySelector(":scope > canvas[data-lumno-bg]");
  if (!canvas) {
    canvas = document.createElement("canvas");
    canvas.dataset.lumnoBg = "true";
    canvas.setAttribute("aria-hidden", "true");
    canvas.style.cssText = "position:absolute;inset:0;width:100%;height:100%;display:block;pointer-events:none;";
    container.prepend(canvas);
  }

  const render = () => {
    const rect = container.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return;
    const cellSize = mode === "dither" ? 2 : 1;
    const cols = Math.max(1, Math.ceil(rect.width / cellSize));
    const rows = Math.max(1, Math.ceil(rect.height / cellSize));
    canvas.width = cols;
    canvas.height = rows;
    canvas.style.imageRendering = mode === "dither" ? "pixelated" : "auto";

    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    paintProceduralScene(ctx, cols, rows, preset, seed);
    const imgData = ctx.getImageData(0, 0, cols, rows);

    if (mode === "dither") {
      const lut = buildBayerLut(
        preset === "nocturne"
          ? { brightness: 0.0, contrast: 1.04, grain: 0.48, levels: 10 }
          : { brightness: 0.01, contrast: 1.08, grain: 0.78, levels: 8 }
      );
      applyBayerLutToImageData(imgData, lut);
    } else {
      applyCanvasGrain(imgData, 0.15, seed);
    }
    ctx.putImageData(imgData, 0, 0);
  };

  render();
  const ro = new ResizeObserver(render);
  ro.observe(container);
  return { rerender: render, destroy: () => ro.disconnect() };
}
