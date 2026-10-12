import { useState } from 'react'
import { Modal, Tabs } from 'antd'
import { ArrowRightOutlined, BookOutlined } from '@ant-design/icons'
import { FEATURE_CATEGORIES } from './featureData'
import './index.css'

interface FeatureIntroProps {
  open: boolean
  onClose: () => void
  isAdmin: boolean
  onNavigate: (path: string) => void
}

export default function FeatureIntro({ open, onClose, isAdmin, onNavigate }: FeatureIntroProps) {
  const [activeTab, setActiveTab] = useState('core-chat')

  const categories = FEATURE_CATEGORIES.filter((c) => !c.adminOnly || isAdmin)

  const handleNavigate = (route: string) => {
    onClose()
    onNavigate(route)
  }

  const tabItems = categories.map((cat) => ({
    key: cat.key,
    label: (
      <span className="feature-intro-tab-label">
        {cat.icon}
        <span className="feature-intro-tab-text">{cat.tab}</span>
      </span>
    ),
    children: (
      <div className="feature-intro-cards-grid">
        {cat.cards.map((card) => (
          <div key={card.title} className="feature-intro-card">
            <div className="feature-intro-card-header">
              <span className="feature-intro-card-icon">{card.icon}</span>
              <span className="feature-intro-card-title">{card.title}</span>
            </div>
            {card.screenshot && (
              <div className="feature-intro-card-screenshot">
                <img
                  src={new URL(`./screenshots/${card.screenshot}`, import.meta.url).href}
                  alt={card.title}
                  loading="lazy"
                  onError={(e) => {
                    ;(e.currentTarget.parentElement as HTMLElement).style.display = 'none'
                  }}
                />
              </div>
            )}
            <p className="feature-intro-card-desc">{card.description}</p>
            {card.route && (
              <button
                className="feature-intro-card-link"
                onClick={() => handleNavigate(card.route!)}
              >
                前往 <ArrowRightOutlined />
              </button>
            )}
          </div>
        ))}
      </div>
    ),
  }))

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width="75vw"
      style={{ maxWidth: 1200, minWidth: 320 }}
      centered
      destroyOnClose
      maskClosable
      title={
        <div className="feature-intro-modal-title">
          <BookOutlined className="feature-intro-modal-title-icon" />
          <div>
            <div className="feature-intro-modal-title-main">功能介绍</div>
            <div className="feature-intro-modal-title-sub">SparkWork 平台功能总览</div>
          </div>
        </div>
      }
      footer={null}
      className="feature-intro-modal"
      zIndex={1060}
    >
      <Tabs
        items={tabItems}
        activeKey={activeTab}
        onChange={setActiveTab}
        tabPosition="left"
        className="feature-intro-tabs"
      />
    </Modal>
  )
}
