import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { getMemoryAccess, listMemories, listMemoryOwners, listMemoryTasks, memoryError, MemoryAccess, MemoryEntry, MemoryOwner, MemoryTask } from '../../api/memory'
import { IN_PROGRESS } from './labels'

export default function useMemoryAdmin() {
  const [search, setSearch] = useSearchParams()
  const initial = Number(search.get('user_id'))
  const [userId, setUserId] = useState<number | null>(Number.isInteger(initial) && initial > 0 ? initial : null)
  const [owners, setOwners] = useState<MemoryOwner[]>([])
  const [entries, setEntries] = useState<MemoryEntry[]>([])
  const [tasks, setTasks] = useState<MemoryTask[]>([])
  const [access, setAccess] = useState<MemoryAccess>({ create: false, review: false, publish: false })
  const [total, setTotal] = useState(0)
  const [taskTotal, setTaskTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [taskPage, setTaskPage] = useState(1)
  const [keyword, setKeyword] = useState('')
  const [status, setStatus] = useState('published')
  const [source, setSource] = useState<string>()
  const [taskStatus, setTaskStatus] = useState<string>()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const ticket = useRef(0)
  const ownerTicket = useRef(0)
  const currentOwner = useRef(userId)
  currentOwner.current = userId
  useEffect(() => {
    let active = true
    listMemoryOwners().then((result) => { if (active) setOwners(result) }).catch((cause) => { if (active) setError(memoryError(cause)) })
    return () => { active = false }
  }, [])
  const searchOwners = async (value: string) => {
    const requestId = ++ownerTicket.current
    try {
      const result = await listMemoryOwners(value)
      if (requestId === ownerTicket.current) setOwners(result)
    } catch (cause) { if (requestId === ownerTicket.current) setError(memoryError(cause)) }
  }
  const reload = useCallback(async () => {
    const requestId = ++ticket.current
    if (!userId) return
    setLoading(true)
    try {
      const [contents, queue, permissions] = await Promise.all([
        listMemories({ user_id: userId, page, keyword, status, source_type: source }),
        listMemoryTasks(userId, taskPage, taskStatus), getMemoryAccess(userId),
      ])
      if (requestId !== ticket.current || currentOwner.current !== userId) return
      setEntries(contents.items); setTotal(contents.total)
      setTasks(queue.items); setTaskTotal(queue.total); setAccess(permissions); setError('')
    } catch (cause) { if (requestId === ticket.current && currentOwner.current === userId) setError(memoryError(cause)) }
    finally { if (requestId === ticket.current) setLoading(false) }
  }, [userId, page, keyword, status, source, taskPage, taskStatus])
  useEffect(() => { void reload(); return () => { ticket.current++ } }, [reload])
  useEffect(() => {
    if (!tasks.some((task) => IN_PROGRESS.includes(task.status))) return
    const timer = window.setInterval(() => { void reload() }, 5000)
    return () => window.clearInterval(timer)
  }, [tasks, reload])
  const changeOwner = (next: number) => {
    ticket.current++; setUserId(next); setSearch({ user_id: String(next) }, { replace: true })
    setPage(1); setTaskPage(1); setEntries([]); setTasks([]); setError('')
    setAccess({ create: false, review: false, publish: false })
  }
  return { userId, owners, entries, tasks, access, total, taskTotal, page, taskPage, keyword, status, source, taskStatus, loading, error,
    setPage, setTaskPage, setKeyword, setStatus, setSource, setTaskStatus, changeOwner, searchOwners, reload, setError }
}
