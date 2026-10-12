import './PlatformLogo.css'

// 中性平台标识：几何纸飞机 mark + TaskPilot 字标（无公司品牌素材）
export type LogoVariant = 'lockup' | 'mark'
export type LogoTone = 'auto' | 'onDark' | 'brand'

interface PlatformLogoProps {
  /** logo height in px（mark 尺寸，字标按比例） */
  size?: number
  /** logo width in px（约束容器宽度，优先于 size） */
  width?: number
  /** lockup = icon + wordmark, mark = icon only (square slots: favicon, collapsed rail) */
  variant?: LogoVariant
  /** auto = ink on light / white chip in dark theme; onDark = white chip; brand = primary color */
  tone?: LogoTone
}

export default function PlatformLogo({
  size = 32,
  width,
  variant = 'lockup',
  tone = 'auto',
}: PlatformLogoProps) {
  const mark = (
    <svg
      className="platform-logo__mark"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <path
        d="M21.5 3.2 2.8 10.1c-.9.3-.9 1.5 0 1.8l4.4 1.6 1.7 5.4c.3.9 1.4 1 1.9.3l2.4-3.1 4.6 3.4c.7.5 1.7.1 1.9-.8l3-14.3c.2-.9-.8-1.6-1.6-1.2Z"
        fill="currentColor"
      />
      <path
        d="M8.6 15.1l-.9 4.6c-.1.6.7 1 1.1.5l3.5-4.1"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
    </svg>
  )
  return (
    <span
      className={`platform-logo platform-logo--${tone} platform-logo--${variant}`}
      style={{ fontSize: `${size}px`, ...(width ? { width } : null) }}
    >
      {mark}
      {variant === 'lockup' && <span className="platform-logo__wordmark">TaskPilot</span>}
    </span>
  )
}
