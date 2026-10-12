import { useCallback, useEffect, useRef, useState } from 'react'
import { Tooltip } from 'antd'
import { ArrowUpOutlined } from '@ant-design/icons'

/**
 * 消息区右下角的滚动圆钮。
 *
 * 规则（与需求一致）：
 *   在顶部   → 箭头向下 → 点击到最底部
 *   在中间   → 箭头向下 → 点击到最底部
 *   在底部   → 箭头向上 → 点击到最顶部
 *   不足一屏 → 不显示
 *
 * 组件只读容器位置、只发滚动指令，不持有页面状态：自动跟随等业务语义由调用方
 * 通过 onScrollToTop / onScrollToBottom 接管，因此可直接复用到其它滚动容器。
 */

export type EdgePosition = 'none' | 'top' | 'middle' | 'bottom'

export interface ScrollMetrics {
  position: EdgePosition
  /** 可滚动总距离：scrollHeight - clientHeight */
  max: number
  /** 距顶距离（已按容器方向归一化） */
  fromTop: number
  /** 距底距离（已按容器方向归一化） */
  fromBottom: number
}

export const DEFAULT_EDGE_THRESHOLD = 24

/** 聊天消息区是 column-reverse：scrollTop=0 在底边，向上滚为负值。 */
function isColumnReverse(el: HTMLElement): boolean {
  return getComputedStyle(el).flexDirection === 'column-reverse'
}

/**
 * 读取滚动位置。把两种滚动方向统一折算成"距顶/距底"两个非负距离，
 * 后续判定与容器方向无关（column-reverse 的负值区间在这里被吸收掉）。
 */
export function readScrollMetrics(
  el: HTMLElement | null,
  threshold: number = DEFAULT_EDGE_THRESHOLD,
): ScrollMetrics {
  if (!el) return { position: 'none', max: 0, fromTop: 0, fromBottom: 0 }
  const max = Math.max(0, el.scrollHeight - el.clientHeight)
  if (max <= threshold) return { position: 'none', max, fromTop: 0, fromBottom: 0 }
  // 距"起点边"的距离：正向容器起点是顶，column-reverse 起点是底
  const fromStart = Math.min(max, Math.abs(el.scrollTop))
  const reverse = isColumnReverse(el)
  const fromBottom = reverse ? fromStart : max - fromStart
  const fromTop = reverse ? max - fromStart : fromStart
  let position: EdgePosition = 'middle'
  if (fromBottom <= threshold) position = 'bottom'
  else if (fromTop <= threshold) position = 'top'
  return { position, max, fromTop, fromBottom }
}

/**
 * 滚到指定边。sign 由容器方向决定；个别浏览器在 column-reverse 下仍用正值区间，
 * 落点不符时按反号纠正一次（仅同步滚动可校验，smooth 由动画自行收敛）。
 */
export function scrollToEdge(
  el: HTMLElement | null,
  edge: 'top' | 'bottom',
  behavior: ScrollBehavior = 'auto',
  threshold: number = DEFAULT_EDGE_THRESHOLD,
): void {
  if (!el) return
  const max = Math.max(0, el.scrollHeight - el.clientHeight)
  if (max <= 0) return
  const wantFromTop = edge === 'bottom' ? max : 0
  const reverse = isColumnReverse(el)
  // 反向容器的 scrollTop 区间是 [-max, 0]，"距顶 wantFromTop" 对应 -(max - wantFromTop)
  const top = reverse ? -(max - wantFromTop) : wantFromTop
  el.scrollTo({ top, behavior })
  if (behavior !== 'auto') return
  const reached = readScrollMetrics(el, threshold)
  if (reached.position !== 'none' && Math.abs(reached.fromTop - wantFromTop) > threshold) {
    el.scrollTo({ top: -top, behavior: 'auto' })
  }
}

interface ScrollEdgeButtonProps {
  /** 滚动容器（结构与 React.RefObject 兼容，不绑定具体元素类型） */
  targetRef: { current: HTMLElement | null }
  /** 内容/尺寸变化提示键，例如 `${messages.length}:${streaming}`；变化即重新测量 */
  refreshKey?: unknown
  /** 到顶/到底的判定容差（px），默认与聊天页自动跟随阈值一致 */
  threshold?: number
  /** 平滑滚动；默认同步跳转（与聊天页既有的 scrollToBottom 行为一致） */
  smooth?: boolean
  /** 自定义滚动动作：聊天页用它顺带维护自动跟随状态 */
  onScrollToTop?: () => void
  onScrollToBottom?: () => void
  className?: string
}

const ScrollEdgeButton = ({
  targetRef,
  refreshKey,
  threshold = DEFAULT_EDGE_THRESHOLD,
  smooth = false,
  onScrollToTop,
  onScrollToBottom,
  className,
}: ScrollEdgeButtonProps) => {
  const [metrics, setMetrics] = useState<ScrollMetrics>({
    position: 'none',
    max: 0,
    fromTop: 0,
    fromBottom: 0,
  })
  const frameRef = useRef(0)
  const boundRef = useRef<HTMLElement | null>(null)
  const [bindVersion, setBindVersion] = useState(0)

  // rAF 节流：流式输出期间 scroll 事件密集，一帧只测一次
  const measure = useCallback(() => {
    if (frameRef.current) return
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = 0
      setMetrics(readScrollMetrics(targetRef.current, threshold))
    })
  }, [targetRef, threshold])

  useEffect(() => {
    const el = targetRef.current
    boundRef.current = el
    measure()
    if (!el) return
    el.addEventListener('scroll', measure, { passive: true })
    // 容器自身尺寸变化（窗口缩放、右侧面板开合）需要重新判定是否可滚
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => {
      el.removeEventListener('scroll', measure)
      observer.disconnect()
      if (frameRef.current) {
        cancelAnimationFrame(frameRef.current)
        frameRef.current = 0
      }
    }
  }, [measure, bindVersion])

  // 内容变化后重新测量；容器被替换（如条件渲染重建）时重绑监听
  useEffect(() => {
    if (boundRef.current !== targetRef.current) setBindVersion((v) => v + 1)
    measure()
  }, [refreshKey, measure, targetRef])

  const atBottom = metrics.position === 'bottom'

  const handleClick = useCallback(() => {
    const behavior: ScrollBehavior = smooth ? 'smooth' : 'auto'
    if (atBottom) {
      if (onScrollToTop) onScrollToTop()
      else scrollToEdge(targetRef.current, 'top', behavior, threshold)
    } else {
      if (onScrollToBottom) onScrollToBottom()
      else scrollToEdge(targetRef.current, 'bottom', behavior, threshold)
    }
    measure()
  }, [atBottom, smooth, onScrollToTop, onScrollToBottom, targetRef, threshold, measure])

  if (metrics.position === 'none') return null

  const label = atBottom ? '回到顶部' : '回到底部'
  return (
    <Tooltip title={label} placement="left">
      <button
        type="button"
        className={`scroll-edge-button${atBottom ? ' at-bottom' : ''}${className ? ` ${className}` : ''}`}
        onClick={handleClick}
        aria-label={label}
      >
        <ArrowUpOutlined className="scroll-edge-icon" />
      </button>
    </Tooltip>
  )
}

export default ScrollEdgeButton
