import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
 Button, Collapse, Drawer, Empty, Input, InputNumber, Modal, Segmented, Slider, Space, Switch,
 Table, Tabs, Tag, Tooltip, Tree, Upload, Progress, message, notification,
} from 'antd'
import {
  DatabaseOutlined, PlusOutlined, DeleteOutlined, EditOutlined, InboxOutlined,
  SearchOutlined, ReloadOutlined, FileTextOutlined, ApartmentOutlined, FolderOutlined, SettingOutlined,
  SyncOutlined, CloseCircleOutlined,
  StopOutlined,
} from '@ant-design/icons'
import { useAuthStore } from '../../store/authStore'
import { hasPerm } from '../../constants/permissions'
import {
  listKnowledgeBases, createKnowledgeBase, updateKnowledgeBase, deleteKnowledgeBase,
  listDocuments, uploadDocuments, deleteDocument, searchKnowledge, softDeleteDocument, hardDeleteDocument, retryDocument,
  restoreDocument,
  listMemoryArchives, restoreMemoryArchive,
  linkVault, reindexVault, getGraph, getVaultTree, getVaultTags, getDoc, getBacklinks,
  getRawText,
  classifySuggest, setDocCategory,
  KnowledgeBaseInfo, KnowledgeDocInfo, MemoryArchiveInfo, GraphData, TreeNode,
} from '../../api/knowledge'
import {
  cancelKnowledgeUpload,
  streamKnowledgeUploadProgress,
  KnowledgeUploadProgress,
} from '../../api/knowledgeProgress'
import GraphView from './components/GraphView'
import DocReader from './components/DocReader'
import './index.css'

// Convert vault TreeNode to Ant Design Tree format with file type icons
function convertTreeNode(node: TreeNode): any {
  const isFile = !!node.doc_id
  const isDeleted = node.status === 'deleted'
  return {
    title: (
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 13, opacity: isDeleted ? 0.4 : 1 }}>
        {isFile
          ? <FileTextOutlined style={{ color: isDeleted ? '#999' : '#1677ff', fontSize: 12 }} />
          : <FolderOutlined style={{ color: '#faad14', fontSize: 12 }} />}
        <span>{node.name}</span>
      </span>
    ),
    key: node.doc_id ? String(node.doc_id) : 'dir:' + (node.name || 'root'),
    children: node.children?.map(convertTreeNode),
  }
}

// Filter tree nodes by search query
function filterTree(nodes: TreeNode[], q: string): TreeNode[] {
  if (!q) return nodes
  const lower = q.toLowerCase()
  return nodes.filter((n) => {
    if (n.name?.toLowerCase().includes(lower)) return true
    if (n.children) return filterTree(n.children, q).length > 0
    return false
  }).map((n) => ({
    ...n,
    children: n.children ? filterTree(n.children, q) : undefined,
  }))
}

const STATUS_META: Record<string, { color: string; label: string }> = {
 uploading: { color: 'default', label: '上传中' }, parsing: { color: 'processing', label: '解析中' },
  converting: { color: 'processing', label: '转换中' }, indexing: { color: 'processing', label: '索引中' },
  extracting: { color: 'processing', label: '提取中' },
  converted: { color: 'blue', label: '已转换' }, ready: { color: 'success', label: '就绪' },
  failed: { color: 'error', label: '失败' },
  canceling: { color: 'warning', label: '取消中' }, canceled: { color: 'default', label: '已取消' },
  deleted: { color: 'default', label: '已删除' },
}
const SCOPE_META: Record<string, { color: string; label: string }> = {
  personal: { color: 'blue', label: '个人' }, shared: { color: 'green', label: '共享' },
}
type ScopeFilter = 'all' | 'personal' | 'shared'

function formatSize(bytes: number): string {
  if (!bytes) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB']; let v = bytes, i = 0
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
 return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${u[i]}`
}

function lsGet<T>(key: string, fallback: T): T {
  try { const v = localStorage.getItem('kg_' + key); return v ? JSON.parse(v) as T : fallback }
  catch { return fallback }
}
function lsSet(key: string, val: any) {
  try { localStorage.setItem('kg_' + key, JSON.stringify(val)) } catch {}
}

// Filter graph to keyword-matched nodes + their direct neighbors (incl. tags)
function filterGraphData(g: GraphData, keyword: string): GraphData {
  const q = keyword.toLowerCase().trim()
  if (!q) return g
  const matched = new Set<string | number>()
  g.nodes.forEach(n => {
    const label = (n.title || n.name || String(n.id)).toLowerCase()
    if (label.includes(q)) matched.add(n.id)
  })
  if (matched.size === 0) return { ...g, nodes: [], edges: [] }
  const keepNodes = new Set(matched)
  const keepEdges = g.edges.filter(e => {
    if (matched.has(e.source) || matched.has(e.target)) {
      keepNodes.add(e.source); keepNodes.add(e.target)
      return true
    }
    return false
  })
  return { ...g, nodes: g.nodes.filter(n => keepNodes.has(n.id)), edges: keepEdges }
}

export default function Knowledge() {
  const { user } = useAuthStore()
  const isAdmin = user?.role === 'admin'
  const canCreateShared = hasPerm(user, 'kb_manage.kb_create')

  const [kbs, setKbs] = useState<KnowledgeBaseInfo[]>([])
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>('all')
  const [selectedKb, setSelectedKb] = useState<KnowledgeBaseInfo | null>(null)
  const [tab, setTab] = useState('docs')
  const [docs, setDocs] = useState<KnowledgeDocInfo[]>([])
  const [loadingKbs, setLoadingKbs] = useState(false)
  const [loadingDocs, setLoadingDocs] = useState(false)
  const [archives, setArchives] = useState<MemoryArchiveInfo[]>([])
  const [loadingArchives, setLoadingArchives] = useState(false)

  // 新建/编辑
  const [editOpen, setEditOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<KnowledgeBaseInfo | null>(null)
  const [editName, setEditName] = useState(''); const [editDesc, setEditDesc] = useState('')
  const [editScope, setEditScope] = useState<'personal' | 'shared'>('personal')
  const [saving, setSaving] = useState(false)

  // vault 绑定
  const [vaultOpen, setVaultOpen] = useState(false)
  const [vaultDir, setVaultDir] = useState('')
  const [linking, setLinking] = useState(false)

  // 图谱
  const [graph, setGraph] = useState<GraphData | null>(null)
  const [gTags, setGTags] = useState<string[]>([])
  const [selTags, setSelTags] = useState<string[]>([])
  const [depth, setDepth] = useState<number>(() => lsGet('depth', 0))
  const [incOrphans, setIncOrphans] = useState(() => lsGet('incOrphans', true))
  const [incTags, setIncTags] = useState(() => lsGet('incTags', true))
  const [loadingGraph, setLoadingGraph] = useState(false)
  const [graphSettingsOpen, setGraphSettingsOpen] = useState(false)
  const [fileSearch, setFileSearch] = useState('')
  const [appliedSearch, setAppliedSearch] = useState('')
 // Graph display settings (Obsidian-style)
  const [nodeSize, setNodeSize] = useState(() => lsGet('nodeSize', 14))
  const [showLabels, setShowLabels] = useState(() => lsGet('showLabels', true))
  const [labelOpacity, setLabelOpacity] = useState(() => lsGet('labelOpacity', 1))
  const [edgeWidth, setEdgeWidth] = useState(() => lsGet('edgeWidth', 1))
  const [showArrows, setShowArrows] = useState(() => lsGet('showArrows', false))
  const [centerForce, setCenterForce] = useState(() => lsGet('centerForce', 1))
  const [repelForce, setRepelForce] = useState(() => lsGet('repelForce', 50))
  const [linkForce, setLinkForce] = useState(() => lsGet('linkForce', 0.1))
  const [linkDistance, setLinkDistance] = useState(() => lsGet('linkDistance', 90))
  const [viewMode, setViewMode] = useState<'reading' | 'graph'>('reading')
  const [searchParams] = useSearchParams()
  useEffect(() => { if (searchParams.get('view') === 'graph') setViewMode('graph') }, [searchParams])

  // 分类
  const [tree, setTree] = useState<TreeNode | null>(null)
  const [tagCloud, setTagCloud] = useState<{ tag: string; count: number }[]>([])

  // DocReader 抽屉
  const [readerDoc, setReaderDoc] = useState<(KnowledgeDocInfo & { content?: string | null }) | null>(null)
  const [backlinks, setBacklinks] = useState<any[]>([])
  const [loadingReader, setLoadingReader] = useState(false)

  // 检索
  const [searchQuery, setSearchQuery] = useState(''); const [searchResults, setSearchResults] = useState<any[] | null>(null)
  const [searching, setSearching] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const hadPendingRef = useRef(false)
  const [progressOpen, setProgressOpen] = useState(false)
  const [batchDocIds, setBatchDocIds] = useState<number[]>([])
  const [activeBatchId, setActiveBatchId] = useState<string | null>(null)
  const [progressData, setProgressData] = useState<KnowledgeUploadProgress | null>(null)
  const [cancellingUpload, setCancellingUpload] = useState(false)
  const [progressStreamKey, setProgressStreamKey] = useState(0)
  const progressAbortRef = useRef<AbortController | null>(null)

 const loadKbs = useCallback(async () => {
    setLoadingKbs(true)
    try {
      const list = await listKnowledgeBases(); setKbs(list)
      setSelectedKb((prev) => prev ? list.find((k) => k.id === prev.id) || list[0] || null : list[0] || null)
    } catch { message.error('加载知识库失败') } finally { setLoadingKbs(false) }
  }, [])

  const loadDocs = useCallback(async (kbId: number) => {
    setLoadingDocs(true)
    try { const l = await listDocuments(kbId); setDocs(l); return l }
    catch { message.error('加载文档列表失败'); return [] } finally { setLoadingDocs(false) }
  }, [])

  const loadArchives = useCallback(async (kbId: number) => {
    setLoadingArchives(true)
    try { setArchives(await listMemoryArchives(kbId)) }
    catch { message.error('加载记忆归档失败') } finally { setLoadingArchives(false) }
  }, [])

  const handleRestoreArchive = async (archiveId: number) => {
    if (!selectedKb) return
    try {
      await restoreMemoryArchive(archiveId)
      message.success('归档已恢复，索引正在更新')
      await Promise.all([loadArchives(selectedKb.id), loadDocs(selectedKb.id)])
    } catch (error: any) {
      message.error(error.response?.data?.detail || '归档恢复失败')
    }
  }

  const loadGraph = useCallback(async (kbId: number) => {
    setLoadingGraph(true)
    try {
      const g = await getGraph(kbId, { tags: selTags.join(','), depth: depth || undefined, include_orphans: incOrphans, include_tags: incTags })
      setGraph(g)
    } catch { message.error('加载图谱失败') } finally { setLoadingGraph(false) }
  }, [selTags, depth, incOrphans, incTags])

  useEffect(() => { loadKbs() }, [loadKbs])
  useEffect(() => {
    if (!selectedKb) { setDocs([]); setGraph(null); return }
  setTab('docs'); setArchives([]); loadDocs(selectedKb.id); setSearchResults(null); setSearchQuery('')
  if (selectedKb.vault_dir) { getVaultTags(selectedKb.id).then(setTagCloud).catch(() => {}); getVaultTree(selectedKb.id).then(setTree).catch(() => {}) }
  }, [selectedKb?.id]) // eslint-disable-line react-hooks/exhaustive-deps
 useEffect(() => {
   if (selectedKb?.vault_dir && viewMode === 'graph') loadGraph(selectedKb.id)
 }, [viewMode, selectedKb?.id, selectedKb?.vault_dir]) // eslint-disable-line react-hooks/exhaustive-deps
  // Re-fetch graph when filter settings (incTags, incOrphans, selTags, depth) change
  useEffect(() => {
    if (selectedKb?.vault_dir && viewMode === 'graph') loadGraph(selectedKb.id)
  }, [loadGraph]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const hasPending = docs.some((d) => ['uploading', 'parsing', 'converting', 'indexing'].includes(d.status))
    if (hasPending && selectedKb) {
      hadPendingRef.current = true
      pollRef.current = setInterval(() => { loadDocs(selectedKb.id) }, 5000)
      return () => { if (pollRef.current) clearInterval(pollRef.current) }
    }
    // All processing done - auto-refresh vault tree and tags once
    if (!hasPending && hadPendingRef.current) {
      hadPendingRef.current = false
      if (selectedKb?.vault_dir) {
        getVaultTree(selectedKb.id).then(setTree).catch(() => {})
        getVaultTags(selectedKb.id).then(setTagCloud).catch(() => {})
      }
      loadKbs()
      const failedN = docs.filter(d => d.status === 'failed').length
      if (failedN > 0) notification.warning({ message: '文档处理完成', description: `${failedN} 篇处理失败，可点顶部红色"失败"按钮查看详情并重试` })
    }
 }, [docs, selectedKb, loadDocs])

  // Upload progress uses SSE; document polling remains as a fallback and final refresh.
  useEffect(() => {
    if (!progressOpen || !activeBatchId) return
    const controller = new AbortController()
    progressAbortRef.current = controller
    let stopped = false
    let terminalSeen = false

    const finish = () => {
      if (terminalSeen || stopped) return
      terminalSeen = true
      if (selectedKb) loadDocs(selectedKb.id)
      loadKbs()
    }

   const run = async () => {
     let pendingEvent: KnowledgeUploadProgress | null = null
     let rafId = 0
     const flushRaf = () => {
       rafId = 0
       if (pendingEvent) {
         setProgressData(pendingEvent)
         pendingEvent = null
       }
     }
     try {
       for await (const event of streamKnowledgeUploadProgress(activeBatchId, controller.signal)) {
         if (stopped) break
         pendingEvent = event
         if (!rafId) {
           requestAnimationFrame(flushRaf)
         }
         // Terminal event: flush immediately to guarantee final render.
         if (event.overall.pending === 0) {
           if (rafId) {
             cancelAnimationFrame(rafId)
             rafId = 0
           }
           if (pendingEvent) {
             setProgressData(pendingEvent)
             pendingEvent = null
           }
           finish()
           break
         }
       }
     } catch (error: any) {
       if (error?.name !== 'AbortError' && !stopped) {
         message.warning('实时进度连接中断，将继续按文档状态刷新')
         setProgressData(null)
       }
     } finally {
       if (rafId) cancelAnimationFrame(rafId)
       if (!stopped) progressAbortRef.current = null
     }
   }
    run()

    return () => {
      stopped = true
      controller.abort()
      progressAbortRef.current = null
    }
  }, [progressOpen, activeBatchId, progressStreamKey, loadDocs, loadKbs]) // eslint-disable-line react-hooks/exhaustive-deps

 // Persist graph settings to localStorage
  useEffect(() => {
    lsSet('depth', depth); lsSet('incOrphans', incOrphans); lsSet('incTags', incTags)
    lsSet('nodeSize', nodeSize); lsSet('showLabels', showLabels); lsSet('labelOpacity', labelOpacity)
    lsSet('edgeWidth', edgeWidth); lsSet('showArrows', showArrows); lsSet('centerForce', centerForce)
    lsSet('repelForce', repelForce); lsSet('linkForce', linkForce); lsSet('linkDistance', linkDistance)
  }, [depth, incOrphans, incTags, nodeSize, showLabels, labelOpacity, edgeWidth, showArrows, centerForce, repelForce, linkForce, linkDistance])

  const filteredKbs = kbs.filter((k) => scopeFilter === 'all' || k.scope === scopeFilter)
  const canManageSelected = !!selectedKb?.can_manage
  const canEditDoc = selectedKb?.scope === 'personal' ? canManageSelected : isAdmin
  const canDeleteDoc = canEditDoc

  const refreshAfterDelete = () => {
    if (!selectedKb) return
    loadDocs(selectedKb.id)
    if (selectedKb?.vault_dir) loadGraph(selectedKb.id)
    if (selectedKb.vault_dir) {
      getVaultTree(selectedKb.id).then(setTree).catch(() => {})
      getVaultTags(selectedKb.id).then(setTagCloud).catch(() => {})
    }
  }
  const handleSoftDelete = async () => {
    if (!readerDoc) return
    try {
      await softDeleteDocument(readerDoc.id)
      message.success('已软删除')
      const data = await getDoc(readerDoc.id)
      setReaderDoc(data)
      refreshAfterDelete()
    }
    catch (e: any) { message.error(e.response?.data?.detail || '删除失败') }
  }
  const handleHardDelete = async () => {
    if (!readerDoc) return
    try { await hardDeleteDocument(readerDoc.id); message.success('已完全删除'); setReaderDoc(null); refreshAfterDelete() }
    catch (e: any) { message.error(e.response?.data?.detail || '删除失败') }
  }
  const handleDocSaved = async (data: any) => {
    setReaderDoc(data)
    if (selectedKb) {
      loadDocs(selectedKb.id)
      if (selectedKb.vault_dir) {
        getVaultTree(selectedKb.id).then(setTree).catch(() => {})
      }
    }
  }
  const handleRestoreDoc = async () => {
    if (!readerDoc) return
    try { await restoreDocument(readerDoc.id); message.success('已恢复'); const data = await getDoc(readerDoc.id); setReaderDoc(data); refreshAfterDelete() }
    catch (e: any) { message.error(e.response?.data?.detail || '恢复失败') }
  }
  const handleRestore = async (rec: KnowledgeDocInfo) => {
    try { await restoreDocument(rec.id); message.success('已恢复'); if (selectedKb) loadDocs(selectedKb.id) }
    catch (e: any) { message.error(e.response?.data?.detail || '恢复失败') }
  }
  const handleRetry = async (rec: KnowledgeDocInfo) => {
    try {
      const result: any = await retryDocument(rec.id)
      if (result?.batch_id) {
        setActiveBatchId(result.batch_id)
        setProgressData(null)
        setCancellingUpload(false)
        setProgressStreamKey((key) => key + 1)
        setProgressOpen(true)
      }
      message.success('已重新加入处理队列')
      if (selectedKb) loadDocs(selectedKb.id)
    }
    catch (e: any) { message.error(e.response?.data?.detail || '重试失败') }
  }
  const handleRetryAll = async () => {
    const scope = batchDocIds.length > 0 ? docs.filter(d => batchDocIds.includes(d.id)) : docs
    const failedDocs = scope.filter(d => d.status === 'failed')
    if (failedDocs.length === 0) return
    let ok = 0
    let lastBatchId: string | null = null
    for (const d of failedDocs) {
      try {
        const result: any = await retryDocument(d.id)
        if (result?.batch_id) lastBatchId = result.batch_id
        ok += 1
      } catch {}
    }
    if (ok > 0) {
      if (lastBatchId) {
        setActiveBatchId(lastBatchId)
        setProgressData(null)
        setCancellingUpload(false)
        setProgressStreamKey((key) => key + 1)
        setProgressOpen(true)
      }
      message.success(`已提交 ${ok} 篇重试`)
      if (selectedKb) loadDocs(selectedKb.id)
    }
  }

  const handleCancelUpload = async () => {
    if (!activeBatchId || cancellingUpload) return
    setCancellingUpload(true)
    try {
      const result = await cancelKnowledgeUpload(activeBatchId)
      setProgressData(result)
      message.success('已请求取消本次上传')
    } catch (e: any) {
      message.error(e.response?.data?.detail || '取消失败')
    } finally {
      setCancellingUpload(false)
    }
  }

  const openCreate = () => { setEditTarget(null); setEditName(''); setEditDesc(''); setEditScope('personal'); setEditOpen(true) }
  const openEdit = (kb: KnowledgeBaseInfo) => { setEditTarget(kb); setEditName(kb.name); setEditDesc(kb.description || ''); setEditScope((kb.scope as any) || 'personal'); setEditOpen(true) }
  const handleSave = async () => {
    if (!editName.trim()) { message.warning('请输入名称'); return }
    const scope = editScope === 'shared' && canCreateShared ? 'shared' : 'personal'
    setSaving(true)
    try {
      if (editTarget) { await updateKnowledgeBase(editTarget.id, { name: editName.trim(), description: editDesc.trim() }); message.success('已更新') }
      else { await createKnowledgeBase({ name: editName.trim(), description: editDesc.trim(), scope }); message.success('已创建') }
      setEditOpen(false); loadKbs()
    } catch (e: any) { message.error(e.response?.data?.detail || '保存失败') } finally { setSaving(false) }
  }
  const handleDeleteKb = (kb: KnowledgeBaseInfo) => {
    Modal.confirm({
      title: `删除「${kb.name}」？`, content: '将删除其全部文档/向量/图谱数据。',
      okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
      onOk: async () => { try { await deleteKnowledgeBase(kb.id); message.success('已删除'); if (selectedKb?.id === kb.id) setSelectedKb(null); loadKbs() } catch (e: any) { message.error(e.response?.data?.detail || '删除失败') } },
    })
  }
  const handleUpload = async (files: File[]) => {
   if (!selectedKb) return
    try {
      const uploaded: any[] = await uploadDocuments(selectedKb.id, files)
      setBatchDocIds(uploaded.map((d) => d.id))
      setActiveBatchId(uploaded.find((d) => d.upload_batch_id)?.upload_batch_id || null)
      setProgressData(null)
      setCancellingUpload(false)
      setProgressStreamKey((key) => key + 1)
      setProgressOpen(true)
      message.success(`已加入队列：${files.length} 个`)
      loadDocs(selectedKb.id)
    }
  catch (e: any) { message.error(e.response?.data?.detail || '上传失败') }
  }
  const handleDeleteDoc = (doc: KnowledgeDocInfo) => {
    Modal.confirm({ title: `删除「${doc.filename}」？`, okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
      onOk: async () => { try { await deleteDocument(doc.id); message.success('已删除'); if (selectedKb) loadDocs(selectedKb.id) } catch (e: any) { message.error(e.response?.data?.detail || '删除失败') } } })
  }
  const handleSearch = async () => {
    if (!selectedKb) return
    if (!searchQuery.trim()) { message.warning('请输入检索内容'); return }
    setSearching(true)
    try { const r = await searchKnowledge(searchQuery.trim(), [selectedKb.id], 5); setSearchResults(Array.isArray(r) ? r : (r.results || [])) }
    catch (e: any) { message.error(e.response?.data?.detail || '检索失败') } finally { setSearching(false) }
  }

  const openReader = async (docId: number) => {
    setLoadingReader(true); setReaderDoc(null); setBacklinks([])
    try {
      const d = await getDoc(docId)
      if (!d.content && !d.is_markdown) {
        const raw = await getRawText(docId)
        d.content = raw.content
      }
      setReaderDoc(d)
      getBacklinks(docId).then(setBacklinks).catch(() => {})
    } catch (e: any) { message.error(e.response?.data?.detail || '打开失败') } finally { setLoadingReader(false) }
  }
  const onWikilink = async (name: string) => {
    if (!selectedKb) return
    const hit = docs.find((d) => (d.title || '') === name || (d.filename || '').replace(/\.md$/i, '') === name)
   if (hit) { openReader(hit.id) } else { message.info(`笔记「${name}」尚不存在（ghost 节点）`) }
 }

  // Open doc from URL ?doc=ID (citation link from Chat page)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const docId = params.get('doc')
    if (docId) {
      openReader(parseInt(docId))
      window.history.replaceState({}, '', window.location.pathname)
    }
  }, [])

  const handleLinkVault = async () => {
    if (!selectedKb) return
    if (!vaultDir.trim()) { message.warning('请输入 vault 目录路径'); return }
    setLinking(true)
    try {
      const r: any = await linkVault(selectedKb.id, vaultDir.trim())
      message.success(`已绑定并索引：${r.indexed} 篇笔记，${r.links} 条链接${r.watcher ? '，已启动监听' : '（监听未启用，需装 watchdog）'}`)
      setVaultOpen(false); setVaultDir('')
      loadDocs(selectedKb.id); loadKbs()
      getVaultTags(selectedKb.id).then(setTagCloud).catch(() => {}); getVaultTree(selectedKb.id).then(setTree).catch(() => {})
    } catch (e: any) { message.error(e.response?.data?.detail || '绑定失败') } finally { setLinking(false) }
  }
  const handleReindex = async () => {
    if (!selectedKb) return
    try { const r: any = await reindexVault(selectedKb.id); message.success(`重索引完成：${r.indexed} 篇，${r.links} 链接`); loadDocs(selectedKb.id); loadKbs() }
    catch (e: any) { message.error(e.response?.data?.detail || '重索引失败') }
  }
  const handleClassify = async (doc: KnowledgeDocInfo) => {
    const hide = message.loading('生成分类建议…', 0)
    try { const r: any = await classifySuggest(doc.id); hide()
      if (!r.category) { message.info(r.reason || '未能生成建议'); return }
      Modal.confirm({ title: '分类建议', content: `${r.category} — ${r.reason || ''}`,
        okText: '采纳', cancelText: '取消',
        onOk: async () => { try { await setDocCategory(doc.id, r.category); message.success('已保存分类'); loadDocs(selectedKb!.id) } catch (e: any) { message.error(e.response?.data?.detail || '保存失败') } } })
    } catch (e: any) { hide(); message.error(e.response?.data?.detail || '建议失败') }
  }

  const docColumns = [
    { title: '文件名', dataIndex: 'filename', key: 'filename', ellipsis: true, render: (v: string, rec: KnowledgeDocInfo) => { const dn = rec.title || v; return rec.is_markdown ? <a onClick={() => openReader(rec.id)}>{dn}</a> : <span><FileTextOutlined style={{ marginRight: 6 }} />{dn}</span> } },
    { title: '大小', dataIndex: 'size', key: 'size', width: 90, render: (v: number) => formatSize(v) },
    { title: '状态', dataIndex: 'status', key: 'status', width: 110, render: (v: string, rec: KnowledgeDocInfo) => { const m = STATUS_META[v] || { color: 'default', label: v }; const t = <Tag color={m.color}>{m.label}</Tag>; return v === 'failed' && rec.error ? <Tooltip title={rec.error}>{t}</Tooltip> : t } },
    { title: '切片', dataIndex: 'chunk_count', key: 'chunk_count', width: 70 },
    { title: '分类', dataIndex: 'category', key: 'category', width: 110, render: (v: string) => v ? <Tag color="green">{v}</Tag> : '—' },
    ...(canManageSelected ? [{ title: '操作', key: 'op', width: 110, render: (_: any, rec: KnowledgeDocInfo) => (<Space size={0}>
      {rec.status === 'failed' && <Button type="text" size="small" onClick={() => handleRetry(rec)}>重试</Button>}
      {rec.status === 'deleted' && <Button type="text" size="small" onClick={() => handleRestore(rec)}>恢复</Button>}
      {rec.is_markdown && <Button type="text" size="small" onClick={() => handleClassify(rec)}>分类</Button>}
      <Button type="text" danger size="small" icon={<DeleteOutlined />} onClick={() => handleDeleteDoc(rec)} />
    </Space>) }] : []),
  ]

  return (
    <div className="knowledge-page">
      <div className="knowledge-header">
        <div className="knowledge-title"><DatabaseOutlined /> 知识库</div>
        <Space>
          {(() => {
            const pendingN = docs.filter((d) => ['uploading', 'parsing', 'converting', 'indexing'].includes(d.status)).length
            const failedN = docs.filter((d) => d.status === 'failed').length
            if (pendingN > 0) return (<Button size="small" icon={<SyncOutlined spin />} onClick={() => setProgressOpen(true)}>{pendingN} 处理中</Button>)
            if (failedN > 0) return (<Button size="small" danger icon={<CloseCircleOutlined />} onClick={() => setProgressOpen(true)}>{failedN} 失败</Button>)
            return null
          })()}
          {selectedKb?.vault_dir && canManageSelected && <Button icon={<ReloadOutlined />} onClick={handleReindex}>重索引</Button>}
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建知识库</Button>
        </Space>
     </div>

      <div className="knowledge-body">
        <div className="knowledge-side">
          <Segmented block size="small" style={{ marginBottom: 12 }} value={scopeFilter} onChange={(v) => setScopeFilter(v as ScopeFilter)}
            options={[{ label: '全部', value: 'all' }, { label: '个人', value: 'personal' }, { label: '共享', value: 'shared' }]} />
          {filteredKbs.length === 0 && !loadingKbs && <Empty description="暂无知识库" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
          {filteredKbs.map((kb) => {
            const s = SCOPE_META[kb.scope || 'shared'] || SCOPE_META.shared
            return (
              <div key={kb.id} className={`knowledge-item ${selectedKb?.id === kb.id ? 'active' : ''}`} onClick={() => setSelectedKb(kb)}>
                <div className="knowledge-item-main">
                  <div className="knowledge-item-name">{kb.name}<Tag color={s.color} style={{ marginLeft: 6, marginRight: 0 }}>{s.label}</Tag>{kb.scope === 'personal' && <Tooltip title="你的个人知识库会自动纳入你所有 Agent 的检索范围，无需在 Agent 上绑定"><Tag color="purple" style={{ marginLeft: 4, marginRight: 0 }}>自动检索</Tag></Tooltip>}</div>
                  {kb.description && <div className="knowledge-item-desc">{kb.description}</div>}
                  <div className="knowledge-item-meta">{isAdmin && kb.owner_name ? `${kb.owner_name} · ` : ''}文档 {kb.doc_count ?? 0} · 就绪 {kb.ready_count ?? 0}{(kb.processing_count ?? 0) > 0 && <span style={{ color: '#1677ff' }}> · 处理中 {kb.processing_count}</span>}{(kb.failed_count ?? 0) > 0 && <span style={{ color: '#ff4d4f' }}> · 失败 {kb.failed_count}</span>}</div>
                </div>
                {kb.can_manage && <div className="knowledge-item-actions" onClick={(e) => e.stopPropagation()}>
                  <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openEdit(kb)} />
                  <Button type="text" size="small" danger icon={<DeleteOutlined />} onClick={() => handleDeleteKb(kb)} />
                </div>}
              </div>
            )
          })}
        </div>

        <div className="knowledge-main">
          {!selectedKb ? <Empty description="请选择或新建知识库" style={{ marginTop: 80 }} /> : selectedKb?.vault_dir && viewMode === 'graph' ? (
            <div style={{ position: 'relative', height: 'calc(100vh - 220px)', border: '1px solid #eee', borderRadius: 6, overflow: 'hidden' }}>
             {graph ? <GraphView data={appliedSearch ? filterGraphData(graph, appliedSearch) : graph} nodeSize={nodeSize} showLabels={showLabels} labelOpacity={labelOpacity} edgeWidth={edgeWidth} showArrows={showArrows} centerForce={centerForce} repelForce={repelForce} linkForce={linkForce} linkDistance={linkDistance} filterText={fileSearch} onNodeDoubleClick={(id) => { setViewMode('reading'); openReader(id) }} /> : <Empty style={{ marginTop: 60 }} description="加载中..." />}
             <Button shape="circle" icon={<SettingOutlined />} type="primary" size="large" style={{ position: 'absolute', top: 52, right: 16, zIndex: 10, boxShadow: '0 2px 8px rgba(0,0,0,0.15)' }} onClick={() => setGraphSettingsOpen(true)} />
             <Button shape="circle" icon={<FileTextOutlined />} size="large" style={{ position: 'absolute', top: 52, left: 16, zIndex: 10, boxShadow: '0 2px 8px rgba(0,0,0,0.15)' }} onClick={() => setViewMode('reading')} />
             {graphSettingsOpen && (
                <div style={{ position: 'absolute', top: 52, right: 16, zIndex: 20, width: 280, maxHeight: 420, overflow: 'auto', background: '#fff', borderRadius: 8, boxShadow: '0 4px 16px rgba(0,0,0,0.15)', padding: '12px 16px', border: '1px solid #eee' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                    <span style={{ fontWeight: 600, fontSize: 13 }}>图谱设置</span>
                    <Button type="text" size="small" onClick={() => setGraphSettingsOpen(false)}>✕</Button>
                  </div>
                <Collapse defaultActiveKey={['1']} ghost items={[
                  { key: '1', label: '过滤器', children: (
                    <>
                     <Input size="small" placeholder="搜索文件..." style={{ marginBottom: 8 }} allowClear value={fileSearch} onChange={(e) => setFileSearch(e.target.value)} />
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}><span>显示标签</span><Switch size="small" checked={incTags} onChange={setIncTags} /></div>
                     <div style={{ display: 'flex', justifyContent: 'space-between' }}><span>显示孤立项</span><Switch size="small" checked={incOrphans} onChange={setIncOrphans} /></div>
                     <Button size="small" type="primary" block style={{ marginTop: 8 }} loading={loadingGraph} onClick={() => setAppliedSearch(fileSearch)}>应用过滤</Button>
                   </>
                 )},
                  { key: '2', label: '社区着色', children: (
                   <div style={{ fontSize: 12, color: '#888' }}>
                     <div>节点按 Louvain 社区检测自动分组着色，右侧图例可点击显示/隐藏社区。</div>
                   </div>
                 )},
                 { key: '3', label: '外观', children: (
                   <>
                     <div style={{ marginBottom: 8 }}>节点大小 <Slider min={6} max={30} value={nodeSize} onChange={setNodeSize} /></div>
                     <div style={{ marginBottom: 8 }}>文本透明度 <Slider min={0} max={1} step={0.1} value={labelOpacity} onChange={setLabelOpacity} /></div>
                     <div>连线粗细 <Slider min={0.5} max={3} step={0.5} value={edgeWidth} onChange={setEdgeWidth} /></div>
                     <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}><span>显示标签</span><Switch size="small" checked={showLabels} onChange={setShowLabels} /></div>
                     <div style={{ display: 'flex', justifyContent: 'space-between' }}><span>显示箭头</span><Switch size="small" checked={showArrows} onChange={setShowArrows} /></div>
                   </>
                 )},
                  { key: '4', label: '力度', children: (
                    <>
                      <div style={{ marginBottom: 8 }}>中心引力 <Slider min={0} max={10} value={centerForce} onChange={setCenterForce} /></div>
                      <div style={{ marginBottom: 8 }}>斥力 <Slider min={0} max={200} value={repelForce} onChange={setRepelForce} /></div>
                      <div style={{ marginBottom: 8 }}>连接力 <Slider min={0} max={1} step={0.05} value={linkForce} onChange={setLinkForce} /></div>
                      <div>连线长度 <Slider min={20} max={200} value={linkDistance} onChange={setLinkDistance} /></div>
                    </>
                  )},
                ]} />
                </div>
              )}
            </div>
          ) : (
            <>
              {selectedKb?.vault_dir && <Button shape="circle" icon={<ApartmentOutlined />} size="large" style={{ position: 'fixed', top: 120, right: 24, zIndex: 1000, boxShadow: '0 2px 8px rgba(0,0,0,0.15)' }} onClick={() => setViewMode('graph')} />}
            <Tabs activeKey={tab} onChange={(key) => {
              setTab(key)
              if (key === 'archives' && selectedKb) void loadArchives(selectedKb.id)
            }} items={[
              { key: 'docs', label: '文档', children: selectedKb?.vault_dir ? (
                <div style={{ display: 'flex', gap: 0, height: 'calc(100vh - 220px)' }}>
                  <div style={{ width: 220, flexShrink: 0, overflow: 'auto', borderRight: '1px solid #e8e8e8', background: '#fafafa', padding: '8px 6px' }}>
                    <Input size="small" prefix={<SearchOutlined />} placeholder="搜索文件..." value={fileSearch} onChange={(e) => setFileSearch(e.target.value)} style={{ marginBottom: 8 }} allowClear />
                    {tree && <Tree treeData={filterTree(tree.children || [], fileSearch).map(convertTreeNode)} onSelect={(keys: any) => { if (keys[0] && !keys[0].startsWith('dir:')) openReader(parseInt(keys[0])) }} selectedKeys={readerDoc ? [String(readerDoc.id)] : []} blockNode showLine={{ showLeafIcon: false }} />}
                    {selectedKb && canManageSelected && (
                      <Upload.Dragger showUploadList={false} multiple beforeUpload={(f: any, fl: any) => { if (fl && fl.length > 0 && fl[0] === f) handleUpload(fl as File[]); else if (!fl || fl.length === 0) handleUpload([f as File]); return false }} style={{ marginTop: 6, borderStyle: 'dashed', borderWidth: 1, borderRadius: 4, padding: '2px 4px', background: 'transparent', height: 24, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                        <span style={{ fontSize: 11, color: '#bbb' }}>+ 上传</span>
                      </Upload.Dragger>
                    )}
                  </div>
                  <div style={{ flex: 1, overflow: 'auto', padding: '0 16px' }}>
                    {readerDoc ? <DocReader doc={readerDoc} backlinks={backlinks} onWikilink={onWikilink} canEdit={canEditDoc} canDelete={canDeleteDoc} onSoftDelete={handleSoftDelete} onHardDelete={handleHardDelete} onSaved={handleDocSaved} onRestore={handleRestoreDoc} /> : <Empty style={{ marginTop: 80 }} description="选择左侧文件查看" />}
                  </div>
                </div>
              ) : (
                <>
                  {canManageSelected && (
                    <Upload.Dragger multiple showUploadList={false} style={{ height: 28, display: 'flex', alignItems: 'center', justifyContent: 'center', borderStyle: 'dashed', borderWidth: 1, borderRadius: 4, marginBottom: 8 }}
                      beforeUpload={(file: any, fileList: any) => { if (fileList && fileList.length > 0 && fileList[0] === file) handleUpload(fileList as File[]); else if (!fileList || fileList.length === 0) handleUpload([file as File]); return false }}>
                      <span style={{ fontSize: 12, color: '#999' }}>+ 拖拽或点击上传</span>
                    </Upload.Dragger>
                  )}
                  <div className="knowledge-doc-head"><span className="knowledge-doc-title">文档列表</span><Button icon={<ReloadOutlined />} size="small" onClick={() => selectedKb && loadDocs(selectedKb.id)}>刷新</Button></div>
                  <Table rowKey="id" size="small" loading={loadingDocs} columns={docColumns as any} dataSource={docs} pagination={false} locale={{ emptyText: '暂无文档' }} rowClassName={(rec: any) => rec.status === 'deleted' ? 'doc-row-deleted' : ''} />
                  <div className="knowledge-search">
                    <div className="knowledge-doc-title">检索测试</div>
                    <Space.Compact style={{ width: '100%' }}>
                      <Input placeholder="输入问题测试检索" value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)} onPressEnter={handleSearch} />
                      <Button type="primary" icon={<SearchOutlined />} loading={searching} onClick={handleSearch}>检索</Button>
                    </Space.Compact>
                    {searchResults && (<div className="knowledge-search-results">
                      {searchResults.length === 0 && <Empty description="未检索到" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
                      {searchResults.map((r, i) => (<div key={i} className="knowledge-search-item">
                        <div className="knowledge-search-meta"><Tag color="blue">{r.filename || `文档#${r.document_id}`}</Tag><span>相关度 {r.score}</span></div>
                        <div className="knowledge-search-text">{r.text}</div></div>))}
                    </div>)}
                  </div>
                </>
              ) },
              ...(selectedKb.kind === 'memory' ? [{
                key: 'archives', label: '七天归档',
                children: <Table rowKey="id" size="small" loading={loadingArchives}
                  dataSource={archives} pagination={{ pageSize: 10 }}
                  locale={{ emptyText: '暂无可恢复的记忆原文' }}
                  columns={[
                    { title: '原始路径', dataIndex: 'source_rel', ellipsis: true },
                    { title: '归档时间', dataIndex: 'absorbed_at', render: (value: string) => value.replace('T', ' ').slice(0, 19) },
                    { title: '可恢复至', dataIndex: 'expires_at', render: (value: string) => value.replace('T', ' ').slice(0, 19) },
                    { title: '操作', key: 'restore', render: (_: unknown, record: MemoryArchiveInfo) =>
                      <Button size="small" onClick={() => void handleRestoreArchive(record.id)}>恢复原文</Button> },
                  ]} />
              }] : []),

            ]} />
          </>
          )}
        </div>
      </div>

      <Drawer open={!!readerDoc && !selectedKb?.vault_dir} onClose={() => setReaderDoc(null)} width={620} title="文档预览" loading={loadingReader}>
        <DocReader doc={readerDoc} backlinks={backlinks} onWikilink={onWikilink} canEdit={canEditDoc} canDelete={canDeleteDoc} onSoftDelete={handleSoftDelete} onHardDelete={handleHardDelete} onSaved={handleDocSaved} onRestore={handleRestoreDoc} />
      </Drawer>

      <Modal title={editTarget ? '编辑知识库' : '新建知识库'} open={editOpen} onCancel={() => setEditOpen(false)} onOk={handleSave} confirmLoading={saving} okText="保存" cancelText="取消">
        <div className="knowledge-form">
          <div className="knowledge-form-label">名称</div>
          <Input value={editName} onChange={(e) => setEditName(e.target.value)} placeholder="如：产品文档库" maxLength={100} />
          <div className="knowledge-form-label">描述（可选）</div>
          <Input.TextArea value={editDesc} onChange={(e) => setEditDesc(e.target.value)} rows={3} maxLength={500} />
          {!editTarget && (<><div className="knowledge-form-label">类型</div>
            <Segmented value={editScope} onChange={(v) => setEditScope(v as any)} options={[{ label: '个人（仅自己与管理员可见）', value: 'personal' }, { label: '共享（全员可见、可被 Agent 绑定）', value: 'shared', disabled: !canCreateShared }]} /></>)}
        </div>
      </Modal>

      <Modal title="绑定 Obsidian vault 目录" open={vaultOpen} onCancel={() => setVaultOpen(false)} onOk={handleLinkVault} confirmLoading={linking} okText="绑定并索引" cancelText="取消">
        <div className="knowledge-form">
          <div className="knowledge-form-label">vault 目录绝对路径</div>
          <Input value={vaultDir} onChange={(e) => setVaultDir(e.target.value)} placeholder="如 /mnt/SparkWork/data/vaults/default" />
          <div style={{ color: '#888', fontSize: 12, marginTop: 8 }}>绑定后该目录的 .md 笔记会被索引建图谱；L3 下 Obsidian 可同时编辑同一目录，变更自动重索引。</div>
        </div>
     </Modal>

      <Modal title="文档处理进度" open={progressOpen} onCancel={() => setProgressOpen(false)} footer={[
        (batchDocIds.length > 0 ? docs.filter(d => batchDocIds.includes(d.id)) : docs).some(d => d.status === 'failed') && (
          <Button key="retryAll" danger onClick={handleRetryAll}>全部重试</Button>
        ),
        activeBatchId && (progressData?.overall.pending ?? 1) > 0 && (
          <Button key="cancel" danger icon={<StopOutlined />} loading={cancellingUpload} onClick={handleCancelUpload}>
            取消上传
          </Button>
        ),
        <Button key="close" onClick={() => setProgressOpen(false)}>关闭</Button>,
      ]} width={560} centered>
        {(() => {
          const batchDocs = batchDocIds.length > 0 ? docs.filter(d => batchDocIds.includes(d.id)) : docs
          const progressRows = activeBatchId && progressData
            ? progressData.documents.map((d) => ({ id: d.id, filename: d.filename, status: d.status, stage: d.stage, percent: d.percent, detail: d.detail, error: d.error || undefined }))
            : batchDocs.map((d) => ({ id: d.id, filename: d.filename, status: d.status, stage: STATUS_META[d.status]?.label || d.status, percent: ['ready', 'converted', 'failed', 'canceled'].includes(d.status) ? 100 : 0, detail: '', error: d.error || undefined }))
          const total = progressRows.length
          const done = progressRows.filter(d => d.status === 'ready' || d.status === 'converted').length
          const pending = progressRows.filter(d => ['uploading', 'parsing', 'converting', 'indexing', 'canceling'].includes(d.status)).length
          const failed = progressRows.filter(d => d.status === 'failed').length
          const canceled = progressRows.filter(d => d.status === 'canceled').length
          const percent = activeBatchId && progressData ? progressData.overall.percent : (total > 0 ? Math.round((done / total) * 100) : 0)
          const allDone = pending === 0
          const activeDocs = progressRows.filter(d => ['uploading', 'parsing', 'converting', 'indexing', 'canceling', 'failed', 'canceled'].includes(d.status))
          return (
            <div>
              <Progress percent={percent} status={allDone ? (failed > 0 ? 'exception' : canceled > 0 ? 'normal' : 'success') : 'active'} />
              <div style={{ marginTop: 12, fontSize: 13, color: '#666' }}>
                {done}/{total} 已完成{pending > 0 ? ` · ${pending} 处理中` : ''}{failed > 0 ? ` · ${failed} 失败` : ''}{canceled > 0 ? ` · ${canceled} 已取消` : ''}
                {allDone && <span style={{ marginLeft: 8, color: failed > 0 ? '#ff4d4f' : canceled > 0 ? '#faad14' : '#52c41a' }}>{failed > 0 ? '处理完成（有失败）' : canceled > 0 ? '处理已取消' : '全部完成'}</span>}
              </div>
              {activeDocs.length > 0 && (
                <div style={{ marginTop: 12, maxHeight: 300, overflow: 'auto' }}>
                  {activeDocs.map(d => {
                    const m = STATUS_META[d.status] || { color: 'default', label: d.status }
                    return (
                      <div key={d.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 0', borderBottom: '1px solid #f5f5f5' }}>
                        <span style={{ minWidth: 0, marginRight: 12 }}>
                          <span style={{ display: 'block', fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 300 }}>{d.filename}</span>
                          {(d.detail || d.percent > 0) && (
                            <span style={{ display: 'block', fontSize: 11, color: '#999', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 300 }}>
                              {d.percent}% · {d.detail || d.stage}
                            </span>
                          )}
                        </span>
                        <span style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                          {d.status === 'failed' && d.error && <Tooltip title={d.error}><Tag color={m.color} style={{ margin: 0 }}>{m.label}</Tag></Tooltip>}
                          {d.status === 'failed' && !d.error && <Tag color={m.color} style={{ margin: 0 }}>{m.label}</Tag>}
                          {d.status !== 'failed' && <Tag color={m.color} style={{ margin: 0 }}>{m.label}</Tag>}
                          {d.status === 'failed' && (
                            <Button type="link" size="small" style={{ padding: 0 }} onClick={() => {
                              const doc = docs.find((item) => item.id === d.id)
                              if (doc) handleRetry(doc)
                            }}>重试</Button>
                          )}
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )
        })()}
      </Modal>
    </div>
  )
}
