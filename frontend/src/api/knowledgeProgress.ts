import request from './request'
import { useAuthStore } from '../store/authStore'
import { getWsBaseUrl } from '../utils/ws'

export interface KnowledgeUploadProgressDoc {
  id: number
  filename: string
  status: string
  stage: string
  percent: number
  detail: string
  error?: string | null
}

export interface KnowledgeUploadProgress {
  batch_id: string
  cancel_requested: boolean
  overall: {
    total: number
    done: number
    failed: number
    canceled: number
    pending: number
    percent: number
  }
  documents: KnowledgeUploadProgressDoc[]
}

export async function cancelKnowledgeUpload(batchId: string): Promise<KnowledgeUploadProgress> {
  return (await request.post(`/knowledge/uploads/${batchId}/cancel`)).data
}

export async function* streamKnowledgeUploadProgress(
  batchId: string,
  signal?: AbortSignal,
): AsyncGenerator<KnowledgeUploadProgress> {
  const token = useAuthStore.getState().token
  if (!token) throw new Error('进度流连接失败：未登录')

  const url = `${getWsBaseUrl()}/api/v1/ws/knowledge-progress/${batchId}?token=${encodeURIComponent(token)}`
  const socket = new WebSocket(url)
 const queue: KnowledgeUploadProgress[] = []
 const waiters: Array<(value?: KnowledgeUploadProgress) => void> = []
 let closed = false

 const finishWaiters = () => {
   // Drain any queued events into pending waiters before signalling
   // closure, so terminal snapshots that arrived during yield are
   // not lost.
   while (queue.length > 0 && waiters.length > 0) {
     const event = queue.shift()!
     const waiter = waiters.shift()!
     waiter(event)
   }
   closed = true
   while (waiters.length > 0) {
     const waiter = waiters.shift()
     waiter?.(undefined)
   }
 }

  const onAbort = () => {
    closed = true
    socket.close()
    finishWaiters()
  }
  signal?.addEventListener('abort', onAbort)

  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      if (data?.type === 'ping') return
      if (data?.type !== 'progress') return
      const progress: KnowledgeUploadProgress = data
      const waiter = waiters.shift()
      if (waiter) waiter(progress)
      else queue.push(progress)
    } catch {
      // Ignore malformed frames; the next frame may still be valid.
    }
  }
  socket.onclose = finishWaiters
  socket.onerror = finishWaiters

  try {
    await new Promise<void>((resolve, reject) => {
      socket.onopen = () => resolve()
      socket.onerror = () => reject(new Error('进度流连接失败'))
    })
    socket.onerror = finishWaiters

    while (!closed) {
      const progress = queue.length > 0 ? queue.shift() : await new Promise<KnowledgeUploadProgress | undefined>((resolve) => {
        waiters.push(resolve)
      })
      if (!progress) break
      yield progress
    }
  } finally {
    signal?.removeEventListener('abort', onAbort)
    socket.close()
  }
}
