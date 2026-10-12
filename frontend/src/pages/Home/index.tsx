import { useEffect, useState, useRef } from 'react'
import { createPortal } from 'react-dom'
import { Button, message, Spin } from 'antd'
import {
  MessageOutlined,
  DownOutlined,
  AppstoreOutlined,
  DatabaseOutlined,
  DashboardOutlined,
  ArrowRightOutlined,
} from '@ant-design/icons'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAuthStore } from '../../store/authStore'
import request from '../../api/request'
import { heroEntrance, entranceStagger, gridEntrance } from '../../lib/motion'
import './index.css'

interface AgentInfo {
  id: number
  name: string
  description: string
  agent_type: string
  is_active: boolean
}

export default function Home() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { user, selectedAgentId, setSelectedAgentId } = useAuthStore()
  const [agents, setAgents] = useState<AgentInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [showAgentMenu, setShowAgentMenu] = useState(false)
  const [menuPos, setMenuPos] = useState({ top: 0, left: 0 })
  const btnRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const dropdownRef = useRef<HTMLDivElement>(null)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    fetchAgents()
  }, [])

  // Open agent selector when navigated with ?open_agent=1
  useEffect(() => {
    if (searchParams.get('open_agent') === '1' && btnRef.current && !loading) {
      const rect = btnRef.current.getBoundingClientRect()
      setMenuPos({ top: rect.bottom + 8, left: rect.left })
      setShowAgentMenu(true)
    }
  }, [searchParams, loading])

  // anime.js entrance: focal display title + staggered supporting elements
  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    const display = root.querySelector('.home-display')
    if (display) heroEntrance(display)
    entranceStagger(Array.from(root.querySelectorAll('.home-anim')), { each: 70 })
    gridEntrance(Array.from(root.querySelectorAll('.home-caps-grid .cap')), { each: 80, from: 'center', grid: [1, 3] })
  }, [])

  const fetchAgents = async () => {
    try {
      const res = await request.get('/agents/')
      setAgents(res.data)
      if (res.data.length > 0 && !selectedAgentId) {
        setSelectedAgentId(res.data[0].id)
      }
    } catch (err) {
      message.error('加载 Agent 失败')
    } finally {
      setLoading(false)
    }
  }

  const handleStartChat = () => {
    if (!selectedAgentId) {
      message.warning('请先选择一个 Agent')
      return
    }
    navigate('/chat')
  }

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node) && !(menuRef.current && menuRef.current.contains(event.target as Node))) {
        setShowAgentMenu(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  const selectedAgent = agents.find((a) => a.id === selectedAgentId)

  return (
    <div className="home-page" ref={rootRef}>
      <section className="home-hero">
        <p className="eyebrow home-anim">SparkWork</p>
        <h1 className="home-display">
          欢迎回来，<span className="home-display-accent">{user?.username}</span>
        </h1>
        <p className="home-sub home-anim">SparkWork 智能体 · 一句提示，一步抵达。</p>

        <div className="home-actions home-anim">
          <div className="agent-selector-wrapper">
            <div ref={dropdownRef} style={{ position: 'relative' }}>
              <Button
                ref={btnRef}
                className="agent-selector-btn"
                size="large"
                onClick={() => {
                  if (btnRef.current) {
                    const rect = btnRef.current.getBoundingClientRect()
                    setMenuPos({ top: rect.bottom + 8, left: rect.left })
                  }
                  setShowAgentMenu(!showAgentMenu)
                }}
              >
                {loading ? (
                  <Spin size="small" />
                ) : selectedAgent ? (
                  <>
                    <span>{selectedAgent.name}</span>
                    <DownOutlined style={{ fontSize: 11 }} />
                  </>
                ) : (
                  <>
                    <span>选择 Agent</span>
                    <DownOutlined style={{ fontSize: 11 }} />
                  </>
                )}
              </Button>
              {showAgentMenu && createPortal(
                <div ref={menuRef} className="custom-dropdown-menu" style={{ position: 'fixed', top: menuPos.top, left: menuPos.left }}>
                  {agents.map((agent) => (
                    <div
                      key={agent.id}
                      className="custom-dropdown-item"
                      onClick={() => {
                        setSelectedAgentId(agent.id)
                        setShowAgentMenu(false)
                      }}
                    >
                      <div>
                        <div style={{ fontWeight: 600, fontSize: 13 }}>{agent.name}</div>
                        <div style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>{agent.description || '暂无描述'}</div>
                      </div>
                    </div>
                  ))}
                </div>,
                document.body
              )}
            </div>
          </div>

          <button className="start-chat-btn" onClick={handleStartChat}>
            <span className="start-chat-btn-content">
              <MessageOutlined />
              <span>开始问答</span>
            </span>
          </button>
        </div>
      </section>

      <section className="home-caps">
        <div className="home-caps-head home-anim">
          <span className="eyebrow">模块</span>
          <h2 className="home-caps-title">快速进入</h2>
        </div>
        <div className="home-caps-grid">
          <div className="cap surface-rail" onClick={() => navigate('/chat')}>
            <div className="cap-icon"><MessageOutlined /></div>
            <div className="cap-body">
              <h3>开始问答</h3>
              <p>与 SparkWork 智能体对话</p>
            </div>
            <ArrowRightOutlined className="cap-arrow" />
          </div>
          <div className="cap surface-line" onClick={() => navigate('/skills')}>
            <div className="cap-icon"><AppstoreOutlined /></div>
            <div className="cap-body">
              <h3>Skill 广场</h3>
              <p>浏览与管理平台技能</p>
            </div>
            <ArrowRightOutlined className="cap-arrow" />
          </div>
          <div className="cap surface-line" onClick={() => navigate('/knowledge')}>
            <div className="cap-icon"><DatabaseOutlined /></div>
            <div className="cap-body">
              <h3>知识库</h3>
              <p>检索与引用知识内容</p>
            </div>
            <ArrowRightOutlined className="cap-arrow" />
          </div>
        </div>
        <div className="cap-flat surface-flat home-anim" onClick={() => navigate('/monitoring')}>
          <span className="cap-flat-left"><DashboardOutlined /> 监控与运维</span>
          <span className="cap-flat-right">查看运行指标 <ArrowRightOutlined /></span>
        </div>
      </section>
    </div>
  )
}
