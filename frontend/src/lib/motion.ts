import { animate, stagger, utils } from 'animejs'
import { useAuthStore } from '../store/authStore'

/** Global motion speed multiplier. 1 = normal; 1.5 = 50% slower. */
export const SPEED = 1.5

/** anime.js v4-aligned easing presets (see examples/advanced-grid-staggering). */
export const EASE = {
  outExpo: 'outExpo',
  outCirc: 'outCirc',
  inOutQuad: 'inOutQuad',
  outElastic: 'outElastic(1, .8)',
} as const

/** App-level motion toggle (authStore.motionEffect) fully overrides the OS
    prefers-reduced-motion signal: on = animate, off = skip. Default on. */
export const prefersReducedMotion = (): boolean => {
  if (typeof window === 'undefined') return true
  return !useAuthStore.getState().motionEffect
}

export type MotionInstance = ReturnType<typeof animate> | null

/* All entrances use explicit from->to ranges instead of a utils.set() pre-pass:
   if the engine ever fails to start, targets keep their natural visible state
   instead of being stuck at opacity 0. */

export function entranceStagger(
  targets: string | Element | Element[],
  opts: { each?: number; from?: 'first' | 'center' | 'last'; y?: number } = {},
): MotionInstance {
  if (prefersReducedMotion() || !targets || (targets as Element[]).length === 0) return null
  return animate(targets, {
    opacity: [0, 1],
    translateY: [opts.y ?? 22, 0],
    scale: [0.97, 1],
    duration: 620 * SPEED,
    ease: EASE.outExpo,
    delay: stagger((opts.each ?? 60) * SPEED, { from: opts.from ?? 'first' }),
  })
}

export function gridEntrance(
  targets: string | Element | Element[],
  opts: { each?: number; from?: 'first' | 'center' | 'last'; grid?: [number, number] } = {},
): MotionInstance {
  if (prefersReducedMotion() || !targets || (targets as Element[]).length === 0) return null
  return animate(targets, {
    opacity: [0, 1],
    translateY: [18, 0],
    scale: [0.96, 1],
    duration: 560 * SPEED,
    ease: EASE.outExpo,
    delay: stagger((opts.each ?? 80) * SPEED, { grid: opts.grid ?? [1, 3], from: opts.from ?? 'center' }),
  })
}

export function heroEntrance(target: string | Element): MotionInstance {
  if (prefersReducedMotion() || !target) return null
  return animate(target, {
    opacity: [0, 1],
    translateY: [26, 0],
    scale: [0.965, 1],
    duration: 900 * SPEED,
    ease: EASE.outElastic,
  })
}

export function slideIndicator(
  indicator: Element,
  to: { top: number; height: number },
): MotionInstance {
  if (!indicator) return null
  if (prefersReducedMotion()) {
    // Jump straight to the target so the sidebar indicator stays correct.
    utils.set(indicator, { top: to.top, height: to.height })
    return null
  }
  return animate(indicator, {
    top: to.top,
    height: to.height,
    duration: 380 * SPEED,
    ease: EASE.outCirc,
  })
}

export function countUp(el: HTMLElement, to: number, duration = 900): MotionInstance {
  if (prefersReducedMotion()) {
    el.textContent = String(to)
    return null
  }
  const obj = { n: 0 }
  return animate(obj, {
    n: to,
    duration: duration * SPEED,
    ease: EASE.outExpo,
    onRender: () => {
      el.textContent = String(Math.round(obj.n))
    },
  })
}
