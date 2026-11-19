export interface BayerDitherOptions {
  cellSize?: number;
  brightness?: number;
  contrast?: number;
  grain?: number;
  levels?: number;
}

export interface LumnoizeOptions {
  mode?: "dither" | "oil-veil";
  cellSize?: number;
  saturationScale?: number;
  shadowLift?: number;
  ivoryTint?: [number, number, number];
  topSkyFade?: boolean;
}

export type LumnoBackgroundPreset =
  | "meadow"
  | "snow-village"
  | "coast"
  | "shanshui"
  | "nocturne";

export interface ProceduralBackgroundOptions {
  preset?: LumnoBackgroundPreset;
  mode?: "dither" | "oil-veil";
  seed?: number;
}

export interface ProceduralBackgroundHandle {
  rerender: () => void;
  destroy: () => void;
}

export function mountLitCardDither(container: HTMLElement): () => void;

export function mountImageBayerDither(
  canvas: HTMLCanvasElement,
  sourceImage: HTMLImageElement,
  options?: BayerDitherOptions
): void;

export function lumnoizeImageToCanvas(
  canvas: HTMLCanvasElement,
  sourceImage: HTMLImageElement,
  options?: LumnoizeOptions
): void;

export function mountProceduralLumnoBackground(
  container: HTMLElement,
  options?: ProceduralBackgroundOptions
): ProceduralBackgroundHandle;
