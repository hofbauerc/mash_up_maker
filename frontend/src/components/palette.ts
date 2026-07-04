// Shared drawing colors for the seam editor canvases.

export const OUT_COLOR = '#ff5a36'
export const IN_COLOR = '#4aa8ff'

// Spectral waveform shades per side: [low, mid, high] stacked from the
// center out — dark saturated bass core, base-color mids, pale highs.
// Kick sections read as thick dark cores; kickless intros as pale wisps.
export const OUT_BAND_COLORS: [string, string, string] = ['#a82f10', '#ff5a36', '#ffc9b8']
export const IN_BAND_COLORS: [string, string, string] = ['#1e63b8', '#4aa8ff', '#c4e0ff']

/** Draw one spectral waveform column: three stacked symmetric bars around
 * cy, band shares of the total height, painted outer(pale)→inner(dark). */
export function drawSpectralColumn(
  ctx: CanvasRenderingContext2D,
  x: number,
  cy: number,
  halfH: number,
  band: number[],
  colors: [string, string, string],
): void {
  const total = band[0] + band[1] + band[2]
  if (total <= 0) return
  const hLow = (band[0] / total) * halfH
  const hMid = hLow + (band[1] / total) * halfH
  ctx.fillStyle = colors[2]
  ctx.fillRect(x, cy - halfH, 1, 2 * halfH)
  ctx.fillStyle = colors[1]
  ctx.fillRect(x, cy - hMid, 1, 2 * hMid)
  ctx.fillStyle = colors[0]
  ctx.fillRect(x, cy - hLow, 1, 2 * hLow)
}

export const SECTION_COLORS: Record<string, string> = {
  intro: '#4aa8ff',
  build: '#ffd24a',
  drop: '#ff5a36',
  break: '#9b7bff',
  outro: '#7dd87d',
}
