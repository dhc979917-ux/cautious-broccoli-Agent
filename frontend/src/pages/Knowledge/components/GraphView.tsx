import { useEffect, useRef, useState, useCallback } from 'react'
import { Network } from 'vis-network'
import { DataSet } from 'vis-data'
import type { GraphData, GraphNode } from '../../../api/knowledge'
import { PRIMARY_COLOR } from '../../../constants/theme'
import './GraphView.css'

const COMMUNITY_COLORS = [
  '#4E79A7', '#F28E2B', '#E15759', '#76B7B2', '#59A14F',
  '#EDC948', '#B07AA1', '#FF9DA7', '#9C755F', '#BAB0AC',
]
const LOD_THRESHOLD = 50

// 个人记忆库节点：按记忆类型固定大小/颜色（后端附 mem_kind）；
// 无 mem_kind 的节点（共享库等）保持原有社区色+度数大小逻辑
const MEM_KIND_STYLE: Record<string, { color: string; scale: number; label: string }> = {
  profile: { color: '#FFB224', scale: 2.5, label: '用户画像' },
  note: { color: '#E5484D', scale: 1.6, label: '笔记' },
  digest_monthly: { color: '#8E4EC6', scale: 2.0, label: '月总结' },
  digest_weekly: { color: '#3B82F6', scale: 1.5, label: '周总结' },
  digest_daily: { color: '#59A14F', scale: 1.0, label: '日总结' },
  daily_index: { color: '#E5E7EB', scale: 0.5, label: '当日索引' },
}
const memStyle = (n: GraphNode) => (n.mem_kind ? MEM_KIND_STYLE[n.mem_kind] : undefined)
const nodeColor = (n: GraphNode) => {
  if (n.is_tag) return '#F472B6'
  const mk = memStyle(n)
  if (mk) return mk.color
  return COMMUNITY_COLORS[(n.community ?? 0) % COMMUNITY_COLORS.length]
}
const nodeRadius = (n: GraphNode, base: number, maxDeg: number) => {
  if (n.is_tag) return base * 0.5  // 标签节点固定小尺寸（直径比 2）
  const mk = memStyle(n)
  if (mk) return Math.max(6, base * mk.scale)
  return base * 0.5 + ((n.degree || 0) / maxDeg) * base * 0.8
}

interface GraphViewProps {
  data: GraphData
  nodeSize: number
  showLabels: boolean
  labelOpacity: number
  edgeWidth: number
  showArrows: boolean
  centerForce: number
  repelForce: number
 linkForce: number
 linkDistance: number
 filterText?: string
 onNodeDoubleClick?: (id: number) => void
}

export default function GraphView({
 data, nodeSize, showLabels, labelOpacity, edgeWidth, showArrows,
  centerForce, repelForce, linkForce, linkDistance, filterText, onNodeDoubleClick,
}: GraphViewProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const networkRef = useRef<any>(null)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const nodesDSRef = useRef<any>(null)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const edgesDSRef = useRef<any>(null)
  const dataRef = useRef(data)
  dataRef.current = data
  const cbRef = useRef(onNodeDoubleClick)
  cbRef.current = onNodeDoubleClick

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<GraphNode[]>([])
  const [hiddenCommunities, setHiddenCommunities] = useState<Set<number>>(new Set())
  const [physicsActive, setPhysicsActive] = useState(true)

  const communities = data.communities || []
  const isLarge = data.nodes.length > LOD_THRESHOLD

  // Build / rebuild graph when data changes
  useEffect(() => {
    if (!containerRef.current) return
    const maxDeg = Math.max(1, ...data.nodes.map(n => n.degree || 0))
    const visNodes = data.nodes.map((n) => {
      const color = nodeColor(n)
      const label = n.title || n.name || String(n.id)
      const deg = n.degree || 0
      const isGhost = !!n.is_ghost
     const showLabel = showLabels && !n.is_pdf && !n.is_ghost
      return {
        id: String(n.id),
        label: showLabel ? label : '',
        color: { background: color, border: isGhost ? '#6b7280' : color, highlight: { background: '#ffffff', border: color } },
        size: Math.max(8, nodeRadius(n, nodeSize, maxDeg)),
        font: { size: showLabel ? 12 : 0, color: '#e0e0e0', strokeWidth: 0 },
        borderWidth: isGhost ? 2 : 1.5,
        ...(isGhost ? { shapeProperties: { borderDashes: [4, 4] } } : {}),
        // 个人记忆图谱：用户画像仅初始位于画布中心，之后交给力导向布局自由排布
        // （画像连边多、度数高，自然会停留在中心附近，且可拖动）
        ...(n.mem_kind === 'profile' ? { x: 0, y: 0 } : {}),
        title: label,
      }
    })
    const seen = new Set<string>()
    const visEdges = data.edges.filter((e) => {
      const k = `${e.source}-${e.target}`
      if (seen.has(k)) return false
      seen.add(k)
      return true
    }).map((e, i) => {
      const isAuto = e.kind === 'auto'
      const isTag = e.kind === 'tag'
      return {
        id: i, from: String(e.source), to: String(e.target), dashes: isAuto,
      width: isTag ? 1 : 1.5,
      color: { color: isTag ? '#F472B6' : isAuto ? '#4a5568' : '#718096', opacity: isAuto ? 0.35 : 0.6, highlight: PRIMARY_COLOR },
      arrows: { to: { enabled: false } },
      }
    })
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const nodesDS: any = new DataSet(visNodes)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const edgesDS: any = new DataSet(visEdges)
    nodesDSRef.current = nodesDS
    edgesDSRef.current = edgesDS
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const network: any = new Network(containerRef.current, { nodes: nodesDS, edges: edgesDS } as any, {
      physics: {
        enabled: true, solver: 'forceAtlas2Based',
        forceAtlas2Based: {
          gravitationalConstant: -repelForce * 0.5, centralGravity: 0.001 * (centerForce || 1),
          springLength: linkDistance, springConstant: 0.01 + (linkForce || 0.1) * 0.08,
          damping: 0.4, avoidOverlap: 0.8,
        },
        stabilization: { iterations: 200, fit: true },
      },
      interaction: { hover: true, tooltipDelay: 100, hideEdgesOnDrag: true, navigationButtons: false, keyboard: false, dragView: true, zoomView: true, dragNodes: true },
      nodes: { shape: 'dot', borderWidth: 1.5 },
      edges: { smooth: { type: 'continuous', roundness: 0.2 }, selectionWidth: 3 },
    } as any)
    networkRef.current = network
    setPhysicsActive(true)
    setPhysicsActive(false)
    network.on('click', (params: any) => {
      if (params.nodes.length > 0) setSelectedNodeId(params.nodes[0])
      else setSelectedNodeId(null)
    })
    network.on('doubleClick', (params: any) => {
      if (params.nodes.length > 0 && cbRef.current) {
        const raw = dataRef.current.nodes.find(n => String(n.id) === String(params.nodes[0]))
        if (raw && typeof raw.id === 'number' && !raw.is_ghost && !raw.is_tag) cbRef.current(raw.id)
      }
    })
    network.on('hoverNode', () => { if (containerRef.current) containerRef.current.style.cursor = 'pointer' })
    network.on('blurNode', () => { if (containerRef.current) containerRef.current.style.cursor = 'default' })
    const onResize = () => { if (networkRef.current) networkRef.current.redraw() }
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      try { network.destroy() } catch { /* noop */ }
      networkRef.current = null
      nodesDSRef.current = null
      edgesDSRef.current = null
    }
  }, [data])

  // Update visual settings (node size, labels, edge width, arrows) without rebuild
  useEffect(() => {
    const ds = nodesDSRef.current
    const net = networkRef.current
    if (!ds || !net) return
    const maxDeg = Math.max(1, ...dataRef.current.nodes.map(n => n.degree || 0))
    const updates = dataRef.current.nodes.map(n => {
      const deg = n.degree || 0
      const showLabel = showLabels && !n.is_pdf && !n.is_ghost
      return {
        id: String(n.id),
        size: Math.max(6, nodeRadius(n, nodeSize, maxDeg)),
        font: { size: showLabel ? 12 : 0, color: `rgba(224,224,224,${labelOpacity})`, strokeWidth: 0 },
      }
    })
    ds.update(updates)
    const eUpdates = dataRef.current.edges.map((_e, i) => ({ id: i, width: edgeWidth, arrows: { to: { enabled: showArrows } } }))
    edgesDSRef.current?.update(eUpdates)
  }, [nodeSize, showLabels, labelOpacity, edgeWidth, showArrows, isLarge])

  // Update physics forces when force settings change
  useEffect(() => {
    const net = networkRef.current
    if (!net) return
    net.setOptions({
      physics: {
        enabled: true, solver: 'forceAtlas2Based',
        forceAtlas2Based: {
          gravitationalConstant: -repelForce * 0.5, centralGravity: 0.001 * (centerForce || 1),
          springLength: linkDistance, springConstant: 0.01 + (linkForce || 0.1) * 0.08,
          damping: 0.4, avoidOverlap: 0.8,
        },
        stabilization: { iterations: 100 },
      },
    } as any)
    setPhysicsActive(true)
    net.stabilize(100)
    setPhysicsActive(false)
  }, [centerForce, repelForce, linkForce, linkDistance])

  // Search
  useEffect(() => {
    if (!searchQuery.trim()) { setSearchResults([]); return }
    const q = searchQuery.toLowerCase().trim()
    setSearchResults(data.nodes.filter(n => (n.title || n.name || String(n.id)).toLowerCase().includes(q)).slice(0, 20))
  }, [searchQuery, data.nodes])

  // Filter/dim nodes based on filterText from settings panel
  useEffect(() => {
    const ds = nodesDSRef.current
    if (!ds) return
    const q = (filterText || '').toLowerCase().trim()
    if (!q) {
      ds.update(dataRef.current.nodes.map(n => ({ id: String(n.id), opacity: 1.0 })))
    } else {
      ds.update(dataRef.current.nodes.map(n => {
        const title = (n.title || n.name || String(n.id)).toLowerCase()
        return { id: String(n.id), opacity: title.includes(q) ? 1.0 : 0.15 }
      }))
    }
  }, [filterText])

  const focusNode = useCallback((nodeId: string | number) => {
    const net = networkRef.current
    if (!net) return
    net.focus(nodeId, { scale: 1.4, animation: true })
    net.selectNodes([String(nodeId)])
    setSelectedNodeId(String(nodeId))
  }, [])

  const toggleCommunity = useCallback((cid: number) => {
    const ds = nodesDSRef.current
    if (!ds) return
    setHiddenCommunities(prev => {
      const next = new Set(prev)
      if (next.has(cid)) next.delete(cid)
      else next.add(cid)
      const updates = dataRef.current.nodes.filter(n => (n.community ?? 0) === cid).map(n => ({ id: String(n.id), hidden: next.has(cid) }))
      ds.update(updates)
      return next
    })
  }, [])

  const toggleAllCommunities = useCallback((hide: boolean) => {
    const ds = nodesDSRef.current
    if (!ds) return
    if (hide) setHiddenCommunities(new Set(communities.map(c => c.cid)))
    else setHiddenCommunities(new Set())
    const updates = dataRef.current.nodes.map(n => ({ id: String(n.id), hidden: hide }))
    ds.update(updates)
  }, [communities])

  const resetLayout = useCallback(() => {
    const net = networkRef.current
    if (!net) return
    net.setOptions({ physics: { enabled: true } } as any)
    setPhysicsActive(true)
    net.stabilize(200)
    setPhysicsActive(false)
  }, [])

  const selectedNode = selectedNodeId ? data.nodes.find(n => String(n.id) === selectedNodeId) || null : null
  const neighbors: GraphNode[] = selectedNode
    ? (() => {
        const seen = new Set<string>(); const result: GraphNode[] = [];
        for (const e of data.edges) {
          if (String(e.source) !== String(selectedNode.id) && String(e.target) !== String(selectedNode.id)) continue;
          const oid = String(e.source) === String(selectedNode.id) ? e.target : e.source;
          const key = String(oid);
          if (seen.has(key)) continue;
          seen.add(key);
          const nb = data.nodes.find(n => String(n.id) === key);
          if (nb) result.push(nb);
        }
        return result;
      })()
    : []
  const allVisible = hiddenCommunities.size === 0
  const someVisible = hiddenCommunities.size > 0 && hiddenCommunities.size < communities.length

  return (
    <div className="graph-view">
      <div className="graph-canvas" ref={containerRef} />
      {isLarge && <div className="graph-lod-badge">{data.nodes.length} 节点 - LOD 模式</div>}
      <div className="graph-sidebar">
        <div className="graph-search-wrap">
          <input className="graph-search" type="text" placeholder="搜索节点..." value={searchQuery} onChange={e => setSearchQuery(e.target.value)} autoComplete="off" />
          {searchResults.length > 0 && (
            <div className="graph-search-results">
              {searchResults.map(n => {
                const color = nodeColor(n)
                return <div key={String(n.id)} className="graph-search-item" style={{ borderLeftColor: color }} onClick={() => { focusNode(n.id); setSearchQuery(''); setSearchResults([]) }}>{n.title || n.name || String(n.id)}</div>
              })}
            </div>
          )}
        </div>
        <div className="graph-info-panel">
          <h3>节点信息</h3>
          {selectedNode ? (
            <div className="graph-info-content">
              <div className="graph-info-field"><b>{selectedNode.title || selectedNode.name || selectedNode.id}</b></div>
              <div className="graph-info-field">类型: {memStyle(selectedNode) ? memStyle(selectedNode)!.label : selectedNode.is_tag ? '标签' : selectedNode.is_ghost ? '幽灵节点' : selectedNode.is_pdf ? 'PDF 文件' : '笔记'}</div>
              <div className="graph-info-field">社区: {selectedNode.community_name || '-'}</div>
              {selectedNode.category && <div className="graph-info-field">分类: {selectedNode.category}</div>}
              {selectedNode.tags && selectedNode.tags.length > 0 && <div className="graph-info-field">标签: {selectedNode.tags.join(', ')}</div>}
              <div className="graph-info-field">度数: {selectedNode.degree ?? 0}</div>
              {neighbors.length > 0 && (
                <>
                  <div className="graph-info-neighbors-label">邻居 ({neighbors.length})</div>
                  <div className="graph-info-neighbors">
                    {neighbors.map(nb => {
                      const color = nodeColor(nb)
                      return <div key={String(nb.id)} className="graph-neighbor-link" style={{ borderLeftColor: color }} onClick={() => focusNode(nb.id)}>{nb.title || nb.name || nb.id}</div>
                    })}
                  </div>
                </>
              )}
            </div>
          ) : (
            <div className="graph-info-empty">点击节点查看详情，双击打开笔记</div>
          )}
        </div>
        <div className="graph-legend-wrap">
          <h3>社区{physicsActive ? ' - 布局中...' : ''}</h3>
          <div className="graph-legend-controls">
            <label>
              <input type="checkbox" checked={allVisible} ref={el => { if (el) el.indeterminate = someVisible }} onChange={e => toggleAllCommunities(!e.target.checked)} />
              全选
            </label>
          </div>
          {communities.map(c => (
            <div key={c.cid} className={`graph-legend-item ${hiddenCommunities.has(c.cid) ? 'dimmed' : ''}`} onClick={() => toggleCommunity(c.cid)}>
              <div className="graph-legend-dot" style={{ background: c.color }} />
              <span className="graph-legend-label">{c.label}</span>
              <span className="graph-legend-count">{c.count}</span>
            </div>
          ))}
        </div>
        <div className="graph-stats">节点 {data.stats.nodes} | 边 {data.stats.edges} | 社区 {communities.length} | ghost {data.stats.ghosts}</div>
      </div>
    </div>
  )
}
