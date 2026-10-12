import { useEffect, useRef } from 'react'

const SIZE = 14
const BORDER = 2
const SPEED_DEG_PER_FRAME = 8 // ~0.8s per revolution at 60fps

/**
 * JS-driven spinning ring. Bypasses CSS animation entirely so that
 * `prefers-reduced-motion: reduce` (which the global stylesheet applies
 * to ALL elements via `*` + `!important`) cannot disable it.
 */
export default function ProgressRing() {
  const ref = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    let angle = 0
    let rafId = 0
    const el = ref.current
    if (!el) return

    const tick = () => {
      angle = (angle + SPEED_DEG_PER_FRAME) % 360
      el.style.transform = `rotate(${angle}deg)`
      rafId = requestAnimationFrame(tick)
    }
    rafId = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(rafId)
  }, [])

  return (
    <span
      ref={ref}
      className="chat-progress-ring"
      aria-hidden="true"
      style={{
        display: 'inline-block',
        boxSizing: 'border-box',
        flex: `0 0 ${SIZE}px`,
        width: SIZE,
        height: SIZE,
        border: `${BORDER}px solid var(--border-color, #e8e8e8)`,
        borderTopColor: 'var(--color-primary, #1677ff)',
        borderRightColor: 'var(--color-primary, #1677ff)',
        borderRadius: '50%',
        willChange: 'transform',
      }}
    />
  )
}
