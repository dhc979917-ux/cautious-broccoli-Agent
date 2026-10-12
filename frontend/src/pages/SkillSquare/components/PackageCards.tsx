import { Button, Empty, Tag, Typography } from 'antd'
import { ArrowLeftOutlined, ToolOutlined } from '@ant-design/icons'
import type { HubResource, HubListing } from '../../../api/skillHub'

type Props = { view: { resource?: HubResource; listing?: HubListing }; onBack: () => void; onPreview: (scope: string) => void }
export default function PackageCards({ view, onBack, onPreview }: Props) {
  const row = view.resource || view.listing!
  const title = view.resource?.title || view.listing?.details.title
  return <section className="hub-package-view" aria-label="技能包成员">
    <div className="hub-package-heading"><Button icon={<ArrowLeftOutlined />} onClick={onBack}>返回技能列表</Button><Typography.Title level={3}>{title}</Typography.Title><Tag>整包 · {row.members?.length || 0} 个技能</Tag></div>
    <Typography.Paragraph type="secondary">包内技能继承整包派生与预览许可；派生、安装、分享仍以整个技能包为单位。</Typography.Paragraph>
    {row.members?.length ? <div className="hub-grid">{row.members.map(member => <article className="hub-resource" key={member.path}><header><div className="hub-resource-icon"><ToolOutlined /></div><Tag>包内技能</Tag></header><h3>{member.title}</h3><p className="hub-summary">{member.summary || '暂无简介'}</p><div className="hub-resource-meta">{member.path === '.' ? '包根目录' : member.path}</div><footer><Button onClick={() => onPreview(member.path)}>查看详情 / 预览</Button></footer></article>)}</div> : <Empty description="当前版本暂无成员技能" />}
  </section>
}
