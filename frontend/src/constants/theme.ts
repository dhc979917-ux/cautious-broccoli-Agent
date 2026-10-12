// Brand color tokens for the TS side (antd tokens, charts, inline styles).
// The CSS-side counterparts live in src/styles/theme.css (--color-primary*);
// keep both in sync when rebranding.
export const PRIMARY_COLOR = '#005BBB'
export const PRIMARY_HOVER = '#004D9F'
export const PRIMARY_ACTIVE = '#003D85'
// lighter tint for accents placed on dark surfaces (login brand panel)
export const PRIMARY_ON_DARK = '#66A9E0'

const PRIMARY_RGB = '0, 91, 187'
const PRIMARY_ON_DARK_RGB = '102, 169, 224'

export const primaryAlpha = (alpha: number) => `rgba(${PRIMARY_RGB}, ${alpha})`
export const primaryOnDarkAlpha = (alpha: number) => `rgba(${PRIMARY_ON_DARK_RGB}, ${alpha})`
