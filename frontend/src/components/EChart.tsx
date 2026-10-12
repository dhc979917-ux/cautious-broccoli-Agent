import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import { useAuthStore } from '../store/authStore'

// echarts 轻封装：跟随明暗主题重建实例，option 变化时整体替换
interface EChartProps {
  // echarts option 结构宽泛，这里用 any 避免各处字面量类型的强约束
  option: any
  height?: number | string
  onEvents?: Record<string, (params: any) => void>
}

export default function EChart({ option, height = 320, onEvents }: EChartProps) {
  const holder = useRef<HTMLDivElement>(null)
  const chart = useRef<echarts.ECharts | null>(null)
  const theme = useAuthStore((s) => s.theme) || 'dark'

  useEffect(() => {
    if (!holder.current) return
    chart.current = echarts.init(holder.current, theme === 'dark' ? 'dark' : undefined)
    const onResize = () => chart.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chart.current?.dispose()
      chart.current = null
    }
  }, [theme])

  useEffect(() => {
    if (!chart.current) return
    chart.current.setOption({ backgroundColor: 'transparent', ...option }, true)
    if (onEvents) {
      Object.entries(onEvents).forEach(([evt, handler]) => {
        chart.current?.off(evt)
        chart.current?.on(evt, handler)
      })
    }
  }, [option, onEvents, theme])

  return <div ref={holder} style={{ width: '100%', height }} />
}
