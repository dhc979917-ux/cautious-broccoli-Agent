import { useCallback, useEffect, useState } from 'react'
import {
  Alert, Button, Drawer, Empty, Form, Input, Modal, Segmented, Space, Switch, Table, Tag, Tooltip, message,
} from 'antd'
import {
  ApiOutlined, PlusOutlined, DeleteOutlined, EditOutlined, StopOutlined, UndoOutlined,
  LinkOutlined, ThunderboltOutlined, UnorderedListOutlined, ReloadOutlined,
} from '@ant-design/icons'
import { useAuthStore } from '../../store/authStore'
import { hasPerm, hasModulePerm } from '../../constants/permissions'
import {
  listMcpServers, createMcpServer, updateMcpServer, deleteMcpServer,
  toggleMcpServer, testMcpConnection, listMcpTools, restoreMcpServer, hardDeleteMcpServer,
  McpServerInfo, McpTestResult,
} from '../../api/mcp'
import './index.css'
import VisibilityDrawer from './VisibilityDrawer'
import { McpSourcePreview } from './Source'
import { readableError, reviewedTools, refreshReviewedTools, updateToolRisk, type ReviewedTool } from '../../api/governance'

interface EnvPair { key: string; value: string; existing?: boolean; rotatedAt?: string | null }

// 表格最小宽度：宽屏按百分比自适应撑满，窄屏收缩到该宽度后横向滚动
const TABLE_MIN_WIDTH = 900

export default function McpTools({ management = false }: { management?: boolean }) {
  const { user } = useAuthStore()
  // 管理操作（增删改/启停 MCP）按 mcp_manage 权限门控，admin 天然拥有
  // 模块级整体显隐 + 各按钮按叶子权限细分
  const isAdmin = management && user?.role === 'admin' && hasModulePerm(user, 'mcp_manage')
  const canCreate = isAdmin && hasPerm(user, 'mcp_manage.create')
  const canUpdate = isAdmin && hasPerm(user, 'mcp_manage.update')
  const canDelete = isAdmin && hasPerm(user, 'mcp_manage.delete.soft')
  const canHardDelete = isAdmin && hasPerm(user, 'mcp_manage.delete.hard')
  const canRestore = isAdmin && hasPerm(user, 'mcp_manage.restore')
  const canToggle = isAdmin && hasPerm(user, 'mcp_manage.toggle')
  const canTest = isAdmin && hasPerm(user, 'mcp_manage.test')
  const [page, setPage] = useState(1)
  const [visibilityServer, setVisibilityServer] = useState<McpServerInfo | null>(null)
  const [sourceTool, setSourceTool] = useState<ReviewedTool | null>(null)

  const [servers, setServers] = useState<McpServerInfo[]>([])
  const [loading, setLoading] = useState(false)

  // 新增/编辑弹窗
  const [editOpen, setEditOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<McpServerInfo | null>(null)
  const [form] = Form.useForm()
  const [mcpType, setMcpType] = useState<'stdio' | 'http'>('stdio')
  const [argsText, setArgsText] = useState('')
  const [envPairs, setEnvPairs] = useState<EnvPair[]>([])
  const [removedEnvKeys, setRemovedEnvKeys] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [testResult, setTestResult] = useState<McpTestResult | null>(null)
  const [testing, setTesting] = useState(false)

  // 工具列表抽屉
  const [toolsOpen, setToolsOpen] = useState(false)
  const [toolsServer, setToolsServer] = useState<McpServerInfo | null>(null)
  const [toolsData, setToolsData] = useState<McpTestResult | null>(null)
  const [loadingTools, setLoadingTools] = useState(false)
  const [catalog, setCatalog] = useState<ReviewedTool[]>([])
  const [catalogError, setCatalogError] = useState('')
  const [refreshingCatalog, setRefreshingCatalog] = useState(false)
  const [destroyTarget, setDestroyTarget] = useState<McpServerInfo | null>(null)
  const [destroyAccount, setDestroyAccount] = useState('')
  const [destroyPassword, setDestroyPassword] = useState('')
  const [destroyStep, setDestroyStep] = useState(1)
  const [destroying, setDestroying] = useState(false)

  const loadServers = useCallback(async () => {
    setLoading(true)
    try {
      const result = await listMcpServers(isAdmin)
      setServers(result)
      setPage((current) => Math.min(current, Math.max(1, Math.ceil(result.length / 8))))
    } catch {
      message.error('加载 MCP 服务失败')
    } finally {
      setLoading(false)
    }
  }, [isAdmin])

  useEffect(() => { loadServers() }, [loadServers])
  useEffect(() => {
    const refresh = () => { if (document.visibilityState === 'visible') void loadServers() }
    window.addEventListener('focus', refresh)
    return () => window.removeEventListener('focus', refresh)
  }, [loadServers])

  // ===== 新增/编辑 =====
  const openCreate = () => {
    setEditTarget(null)
    setMcpType('stdio')
    setArgsText('')
    setEnvPairs([])
    setRemovedEnvKeys([])
    setTestResult(null)
    form.resetFields()
    form.setFieldsValue({ type: 'stdio', enabled: true })
    setEditOpen(true)
  }

  const openEdit = (s: McpServerInfo) => {
    setEditTarget(s)
    setMcpType(s.type)
    setArgsText((s.args || []).join('\n'))
    setEnvPairs(Object.entries(s.env || {}).map(([key, status]) => ({
      key, value: '', existing: status.configured, rotatedAt: status.last_rotated_at,
    })))
    setRemovedEnvKeys([])
    setTestResult(null)
    form.resetFields()
    form.setFieldsValue({
      name: s.name, type: s.type, command: s.command, url: s.url,
      description: s.description, enabled: s.enabled,
    })
    setEditOpen(true)
  }

  const buildPayload = () => {
    const v = form.getFieldsValue()
    const args = argsText.split('\n').map((s) => s.trim()).filter(Boolean)
    const env: Record<string, string | null> = {}
    removedEnvKeys.forEach((key) => { env[key] = null })
    envPairs.forEach((pair) => {
      if (pair.key.trim() && pair.value) env[pair.key.trim()] = pair.value
    })
    return {
      name: (v.name || '').trim(),
      type: mcpType,
      command: mcpType === 'stdio' ? (v.command || '').trim() : null,
      url: mcpType === 'http' ? (v.url || '').trim() : null,
      description: (v.description || '').trim() || null,
      args: mcpType === 'stdio' ? args : [],
      env: mcpType === 'stdio' ? env : editTarget && editTarget.type === 'stdio'
        ? Object.fromEntries(Object.keys(editTarget.env || {}).map((key) => [key, null])) : {},
      enabled: v.enabled !== false,
    }
  }

  const validatePayload = (p: any): string | null => {
    if (!p.name) return '请输入服务名称'
    if (p.type === 'stdio' && !p.command) return 'stdio 类型必须填写 command'
    if (p.type === 'http' && !p.url) return 'http 类型必须填写 URL'
    const names = envPairs.map((pair) => pair.key.trim()).filter(Boolean)
    if (new Set(names).size !== names.length) return '环境变量名称不能重复'
    if (envPairs.some((pair) => pair.key.trim() && !pair.existing && !pair.value)) return '新环境变量必须填写值'
    return null
  }

  const handleSave = async () => {
    const payload = buildPayload()
    const err = validatePayload(payload)
    if (err) { message.warning(err); return }
    setSaving(true)
    try {
      if (editTarget) {
        await updateMcpServer(editTarget.id, payload)
        message.success('已更新')
      } else {
        await createMcpServer(payload)
        message.success('已添加')
      }
      setEditOpen(false)
      loadServers()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async () => {
    const payload = buildPayload()
    const err = validatePayload(payload)
    if (err) { message.warning(err); return }
    setTesting(true)
    setTestResult(null)
    try {
      const res = await testMcpConnection(payload, editTarget?.id)
      setTestResult(res)
      if (res.ok) message.success(`连接成功，发现 ${res.tool_count} 个工具`)
    } catch (e: any) {
      setTestResult({ ok: false, tool_count: 0, tools: [], error: e.response?.data?.detail || '测试失败' })
    } finally {
      setTesting(false)
    }
  }

  const handleDelete = (s: McpServerInfo) => {
    Modal.confirm({
      title: `删除 MCP 服务「${s.name}」？`,
      content: '服务将停用并从聊天可选列表移除，配置和历史记录保留，可恢复。',
      okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
      onOk: async () => {
        try {
          await deleteMcpServer(s.id)
          message.success('已软删除')
          loadServers()
        } catch (e: any) {
          message.error(e.response?.data?.detail || '删除失败')
          throw e
        }
      },
    })
  }

  const closeDestroy = () => { setDestroyTarget(null); setDestroyAccount(''); setDestroyPassword(''); setDestroyStep(1) }
  const hardDelete = async () => {
    if (!destroyTarget) return
    setDestroying(true)
    try { await hardDeleteMcpServer(destroyTarget.id, destroyAccount, destroyPassword); message.success('已彻底删除'); closeDestroy(); await loadServers() }
    catch (cause) { message.error(readableError(cause)) }
    finally { setDestroying(false) }
  }
  const restore = async (server: McpServerInfo) => {
    try { await restoreMcpServer(server.id); await loadServers(); message.success('已恢复，请核对配置后启用') }
    catch (cause) { message.error(readableError(cause)) }
  }

  const handleToggle = async (s: McpServerInfo) => {
    try {
      await toggleMcpServer(s.id)
      loadServers()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '操作失败')
    }
  }

  const openTools = async (s: McpServerInfo) => {
    setToolsServer(s)
    setToolsOpen(true)
    setToolsData(null)
    setCatalog([])
    setCatalogError('')
    setLoadingTools(true)
    try {
      if (isAdmin) {
        setCatalog(await reviewedTools(s.id))
      } else {
        setToolsData(await listMcpTools(s.id))
      }
    } catch (e: any) {
      setCatalogError(readableError(e))
    } finally {
      setLoadingTools(false)
    }
  }

  const refreshCatalog = async () => {
    if (!toolsServer) return
    setRefreshingCatalog(true)
    setCatalogError('')
    try {
      const result = await refreshReviewedTools(toolsServer.id)
      setCatalog(await reviewedTools(toolsServer.id))
      message.info(result.changes.length ? `发现 ${result.changes.length} 项工具变化，受影响授权需复核` : '工具目录未变化')
    } catch (error) {
      setCatalogError(readableError(error))
    } finally {
      setRefreshingCatalog(false)
    }
  }

  const setRisk = async (tool: ReviewedTool, risk: 'low' | 'high') => {
    try {
      await updateToolRisk(tool.server_id, tool.name, tool.revision, risk)
      setCatalog(await reviewedTools(tool.server_id))
      message.success('风险分类已更新；Tool 授权仍需单独设置')
    } catch (error) {
      setCatalogError(readableError(error))
    }
  }

  const columns = [
    {
      title: '名称', dataIndex: 'name', key: 'name', width: '20%', align: 'left' as const,
      render: (v: string, rec: McpServerInfo) => (
        <span className="mcp-name">
          <ApiOutlined style={{ marginRight: 6, color: rec.enabled ? 'var(--color-primary)' : 'var(--text-disabled)' }} />
          {v}
        </span>
      ),
    },
    {
      title: '类型', dataIndex: 'type', key: 'type', width: '12%', align: 'center' as const,
      render: (v: string) => <Tag color={v === 'stdio' ? 'geekblue' : 'purple'}>{v}</Tag>,
    },
    {
      title: '连接', key: 'conn', ellipsis: true, align: 'center' as const,
      render: (_: any, rec: McpServerInfo) => {
        const text = !isAdmin ? rec.connection : rec.type === 'stdio'
          ? `${rec.command || ''} ${(rec.args || []).join(' ')}`.trim()
          : rec.url
        return <span className="mcp-conn" title={text || ''}>{text || '—'}</span>
      },
    },
    {
      title: '启用', dataIndex: 'enabled', key: 'enabled', width: '8%', align: 'center' as const,
      render: (v: boolean, rec: McpServerInfo) => rec.is_deleted ? <Tag>已软删除</Tag> : canToggle
        ? <Switch size="small" checked={v} onChange={() => handleToggle(rec)} />
        : <Tag color={v ? 'success' : 'default'}>{v ? '启用' : '停用'}</Tag>,
    },
    {
      title: '隐藏', key: 'visibility', width: '18%', align: 'center' as const,
      render: (_: any, rec: McpServerInfo) => rec.is_deleted ? '—' : <Button type="text" size="small"
        aria-label={`配置 ${rec.name} 可见范围`} onClick={() => setVisibilityServer(rec)}>
        <Tag color={rec.visibility?.hidden ? 'gold' : 'blue'}>{rec.visibility?.hidden ? '限定可见' : '全部可见'}</Tag>配置
      </Button>,
    },
    {
      title: '操作', key: 'op', width: isAdmin ? 150 : 90, align: 'center' as const,
      render: (_: any, rec: McpServerInfo) => (
        <Space size={0} wrap>
          {!rec.is_deleted && <Tooltip title="查看工具"><Button type="text" size="small" aria-label="查看工具" icon={<UnorderedListOutlined />} onClick={() => openTools(rec)} /></Tooltip>}
          {(canUpdate || canDelete) && (
            <>
              {canUpdate && !rec.is_deleted && <Tooltip title="编辑"><Button type="text" size="small" icon={<EditOutlined />} onClick={() => openEdit(rec)} /></Tooltip>}
              {canDelete && !rec.is_deleted && <Button type="text" size="small" danger aria-label="删除" title="删除" icon={<DeleteOutlined />} onClick={() => handleDelete(rec)} />}
            </>
          )}
          {canRestore && rec.is_deleted && <Button type="text" size="small" icon={<UndoOutlined />} onClick={() => void restore(rec)}>恢复</Button>}
          {canHardDelete && <Tooltip title={rec.is_deleted ? '彻底删除，不可恢复' : '请先删除，再彻底删除'}><Button type="text" size="small" danger aria-label="彻底删除" icon={<StopOutlined />} disabled={!rec.is_deleted} onClick={() => { setDestroyTarget(rec); setDestroyStep(1) }} /></Tooltip>}
        </Space>
      ),
    },
  ]

  if (management && !isAdmin) return <Alert type="warning" showIcon message="仅管理员可进入 MCP 管理后台。" />
  const displayedColumns = isAdmin ? columns : columns.filter((column) => !['enabled', 'visibility'].includes(column.key))
  return (
    <div className={`mcp-page ${isAdmin ? 'mcp-management' : 'mcp-directory'}`}>
      <div className="mcp-header">
        <div><h2 className="mcp-title"><ApiOutlined /> MCP 工具</h2>
          <div className="mcp-tip">{isAdmin ? 'MCP 服务由管理员配置，管理连接、可见范围与工具风险。' : '浏览可用的 MCP 服务，查看工具及功能说明。'}</div></div>
        {canCreate && <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>添加 MCP</Button>}
      </div>
      <Table
        rowKey="id"
        size="small"
        loading={loading}
        columns={displayedColumns as any}
        dataSource={servers}
        pagination={{ current: page, pageSize: 8, onChange: setPage, showSizeChanger: false, showTotal: (total) => `共 ${total} 个 MCP` }}
        locale={{ emptyText: '暂无 MCP 服务' }}
        tableLayout="fixed"
        scroll={{ x: isAdmin ? TABLE_MIN_WIDTH : 600 }}
      />
      <VisibilityDrawer server={visibilityServer} onClose={() => setVisibilityServer(null)} onSaved={loadServers} />
      <Modal title={`Python 源码 · ${sourceTool?.name || ''}`} open={!!sourceTool} centered
        width="min(1400px, 94vw)" zIndex={1600} footer={null} destroyOnClose className="mcp-source-modal"
        onCancel={() => setSourceTool(null)}>
        {sourceTool && <McpSourcePreview serverId={sourceTool.server_id} toolName={sourceTool.name} />}
      </Modal>

      <Modal title={`彻底删除 MCP「${destroyTarget?.name || ''}」`} open={!!destroyTarget} onCancel={closeDestroy} destroyOnClose
        footer={<Space><Button disabled={destroying} onClick={closeDestroy}>取消</Button>{destroyStep === 1
          ? <Button danger type="primary" disabled={!destroyAccount || !destroyPassword} onClick={() => setDestroyStep(2)}>下一步</Button>
          : <Button danger type="primary" loading={destroying} onClick={() => void hardDelete()}>确认彻底删除</Button>}</Space>}>
        <Alert type="warning" showIcon message="不可恢复；必须先解除助手及待应用配置的绑定。历史审计保留。" />
        {destroyStep === 1 ? <Form layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item label="管理员账号" htmlFor="mcp-destroy-account"><Input id="mcp-destroy-account" autoComplete="off" value={destroyAccount} onChange={(event) => setDestroyAccount(event.target.value)} /></Form.Item>
          <Form.Item label="管理员密码" htmlFor="mcp-destroy-password"><Input.Password id="mcp-destroy-password" autoComplete="new-password" value={destroyPassword} onChange={(event) => setDestroyPassword(event.target.value)} /></Form.Item>
        </Form> : <p>确认永久删除「{destroyTarget?.name}」及其工具目录、授权和审批记录？</p>}
      </Modal>
      {/* 新增/编辑弹窗 */}
      <Modal
        title={editTarget ? '编辑 MCP 服务' : '添加 MCP 服务'}
        open={editOpen}
        onCancel={() => setEditOpen(false)}
        width="min(640px, 92vw)"
        footer={[
          ...(canTest ? [<Button key="test" icon={<ThunderboltOutlined />} loading={testing} onClick={handleTest}>测试连接</Button>] : []),
          <Button key="cancel" onClick={() => setEditOpen(false)}>取消</Button>,
          <Button key="save" type="primary" loading={saving} onClick={handleSave}>保存</Button>,
        ]}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="如：文件系统 MCP" maxLength={100} />
          </Form.Item>
          <Form.Item label="类型">
            <Segmented
              value={mcpType}
              onChange={(v) => setMcpType(v as 'stdio' | 'http')}
              options={[{ label: 'stdio（本地命令）', value: 'stdio' }, { label: 'http（远程 URL）', value: 'http' }]}
              block
            />
          </Form.Item>
          {mcpType === 'stdio' ? (
            <>
              <Form.Item name="command" label="命令（command）" rules={[{ required: true, message: '请输入命令' }]}>
                <Input placeholder="如：npx 或 python" />
              </Form.Item>
              <Form.Item label="参数（args，每行一个）">
                <Input.TextArea
                  value={argsText}
                  onChange={(e) => setArgsText(e.target.value)}
                  rows={2}
                  placeholder={'-y\n@modelcontextprotocol/server-filesystem'}
                />
              </Form.Item>
              <Form.Item label="环境变量（env）">
                <div className="mcp-env">
                  {envPairs.map((p, i) => (
                    <div key={i} className="mcp-env-row">
                      <Input
                        size="small" placeholder="KEY" value={p.key} disabled={p.existing}
                        aria-label="环境变量名称"
                        onChange={(e) => setEnvPairs(envPairs.map((x, j) => j === i ? { ...x, key: e.target.value } : x))}
                      />
                      <Input.Password
                        size="small" placeholder={p.existing ? '留空保留，输入新值轮换' : '输入密钥'} value={p.value}
                        aria-label={`${p.key || '新环境变量'}密钥`}
                        onChange={(e) => setEnvPairs(envPairs.map((x, j) => j === i ? { ...x, value: e.target.value } : x))}
                      />
                      {p.existing && <Tooltip title={p.rotatedAt ? `最近轮换：${p.rotatedAt}` : '已配置'}><Tag>已配置</Tag></Tooltip>}
                      <Button size="small" type="text" danger icon={<DeleteOutlined />}
                        aria-label={`移除${p.key || '新'}环境变量`}
                        onClick={() => {
                          if (p.existing) setRemovedEnvKeys([...removedEnvKeys, p.key])
                          setEnvPairs(envPairs.filter((_, index) => index !== i))
                        }} />
                    </div>
                  ))}
                  <Button size="small" icon={<PlusOutlined />} onClick={() => setEnvPairs([...envPairs, { key: '', value: '' }])}>
                    添加环境变量
                  </Button>
                </div>
              </Form.Item>
            </>
          ) : (
            <Form.Item name="url" label="服务 URL" rules={[{ required: true, message: '请输入 URL' }]}>
              <Input placeholder="如：http://localhost:8080/sse" />
            </Form.Item>
          )}
          <Form.Item name="description" label="描述（可选）">
            <Input.TextArea rows={2} maxLength={500} />
          </Form.Item>
          {testResult && (
            <div className={`mcp-test ${testResult.ok ? 'ok' : 'fail'}`}>
              {testResult.ok
                ? <><LinkOutlined /> 连接成功，发现 {testResult.tool_count} 个工具</>
                : <>连接失败：{testResult.error}</>}
            </div>
          )}
        </Form>
      </Modal>

      {/* 工具列表抽屉 */}
      <Drawer
        title={`工具列表 · ${toolsServer?.name || ''}`}
        extra={isAdmin && <Button size="small" icon={<ReloadOutlined />} onClick={() => void refreshCatalog()}
          loading={refreshingCatalog} disabled={!toolsServer?.enabled}>重新枚举并检查差异</Button>}
        open={toolsOpen}
        onClose={() => setToolsOpen(false)}
        width="min(640px, 94vw)"
        className="mcp-tools-drawer"
      >
        <section className="mcp-server-overview"><span>MCP 服务说明</span><p>{toolsServer?.display_description || toolsServer?.description || '暂未填写服务描述'}</p></section>
        {loadingTools && <div className="mcp-loading">载入工具目录中…</div>}
        {catalogError && <Alert type="error" showIcon message={catalogError} />}
        {isAdmin && <>
          {!toolsServer?.enabled && <Alert type="warning" showIcon message="服务已停用，不可调用工具。" />}
          {!catalog.length && !loadingTools && <Empty description="暂无已审目录；请先在受控环境刷新" />}
          {catalog.map((tool) => <section key={tool.name} className="mcp-tool-item">
            <div className="mcp-tool-heading"><button type="button" className="mcp-tool-name mcp-source-trigger" onClick={() => setSourceTool(tool)} title="查看 Python 源码">{tool.name}</button><Tag color={!tool.available ? 'default' : tool.risk === 'high' ? 'red' : tool.risk === 'low' ? 'green' : 'orange'}>
              {!tool.available ? '下线' : tool.risk === 'unknown' ? '待分类' : tool.risk === 'high' ? '高风险' : '低风险'}
            </Tag></div>
            {tool.description && <div className="mcp-tool-desc">{tool.description}</div>}
            <details className="mcp-tool-schema"><summary>查看输入／输出</summary><pre className="governance-result">{JSON.stringify({ input: tool.input_schema, output: tool.output_schema }, null, 2)}</pre></details>
            {tool.available && <Space style={{ marginTop: 10 }}><Button size="small" danger onClick={() => void setRisk(tool, 'high')}>标为高风险</Button>
              <Button size="small" onClick={() => void setRisk(tool, 'low')}>审核为低风险</Button></Space>}
          </section>)}
        </>}
        {!loadingTools && toolsData && !toolsData.ok && (
          <Empty description={toolsData.error || '连接失败'} image={Empty.PRESENTED_IMAGE_SIMPLE} />
        )}
        {!loadingTools && toolsData?.ok && toolsData.tools.length === 0 && (
          <Empty description="该服务未暴露工具" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        )}
        {!loadingTools && toolsData?.ok && toolsData.tools.map((t) => (
          <div key={t.name} className="mcp-tool-item">
            <div className="mcp-tool-name">{t.name}</div>
            {t.description && <div className="mcp-tool-desc">{t.description}</div>}
          </div>
        ))}
      </Drawer>
    </div>
  )
}
