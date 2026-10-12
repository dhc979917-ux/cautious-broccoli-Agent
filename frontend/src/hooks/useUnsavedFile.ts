import { useContext, useEffect } from 'react'
import { UNSAFE_NavigationContext } from 'react-router-dom'

export default function useUnsavedFile(dirty: boolean) {
  const { navigator } = useContext(UNSAFE_NavigationContext)
  useEffect(() => {
    if (!dirty) return
    const ask = () => window.confirm('文件修改尚未保存，确定离开？')
    const originalPush = navigator.push
    const originalReplace = navigator.replace
    const originalGo = navigator.go
    navigator.push = (...args) => { if (ask()) originalPush.apply(navigator, args) }
    navigator.replace = (...args) => { if (ask()) originalReplace.apply(navigator, args) }
    navigator.go = (...args) => { if (ask()) originalGo.apply(navigator, args) }
    const index = window.history.state?.idx
    let restoring = false
    const pop = (event: PopStateEvent) => {
      if (restoring) { restoring = false; event.stopImmediatePropagation(); return }
      if (ask()) return
      const nextIndex = event.state?.idx
      if (typeof index === 'number' && typeof nextIndex === 'number') {
        event.stopImmediatePropagation()
        restoring = true
        window.history.go(index - nextIndex)
      }
    }
    const unload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('popstate', pop, true)
    window.addEventListener('beforeunload', unload)
    return () => {
      navigator.push = originalPush
      navigator.replace = originalReplace
      navigator.go = originalGo
      window.removeEventListener('popstate', pop, true)
      window.removeEventListener('beforeunload', unload)
    }
  }, [dirty, navigator])
}
