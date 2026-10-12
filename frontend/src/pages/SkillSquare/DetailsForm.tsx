import { Button, Form, Input, Select, Space } from 'antd'
import type { HubDetails } from '../../api/skillHub'
export const blankDetails = (title = '', summary = ''): HubDetails => ({ title, summary, category: '其他', tags: [], description: '', example: '', requirements: '', risks: '', license: '', dependencies: {} })
export const categories = ['办公效率', '数据分析', '开发工具', '文档处理', '行业应用', '其他']
export default function DetailsForm({ value, onSave, busy, action = '保存说明为新草稿' }: { value: HubDetails; onSave: (value: HubDetails) => void; busy: boolean; action?: string }) {
  return <Form layout="vertical" initialValues={value} key={JSON.stringify(value)} onFinish={onSave}>
    <Form.Item name="title" label="标题" rules={[{ required: true }]}><Input maxLength={200} /></Form.Item>
    <Form.Item name="summary" label="简介"><Input.TextArea rows={2} maxLength={2000} /></Form.Item>
    <Space align="start" className="hub-form-pair"><Form.Item name="category" label="分类"><Select options={categories.map(x => ({ label: x, value: x }))} /></Form.Item><Form.Item name="tags" label="标签"><Select mode="tags" maxTagCount={4} /></Form.Item></Space>
    <Form.Item name="description" label="详细说明"><Input.TextArea rows={4} maxLength={20000} /></Form.Item>
    <Form.Item name="example" label="使用示例（社区投稿必填）"><Input.TextArea rows={3} maxLength={4000} /></Form.Item>
    <Form.Item name="requirements" label="使用前提与所需权限"><Input.TextArea rows={2} maxLength={4000} /></Form.Item>
    <Form.Item name="risks" label="外部访问、文件操作等风险说明"><Input.TextArea rows={2} maxLength={4000} /></Form.Item>
    <Form.Item name="license" label="来源、署名与许可（社区投稿必填）"><Input.TextArea rows={2} maxLength={4000} /></Form.Item>
    <Form.Item name="icon" label="图标标识（可选）"><Input maxLength={100} placeholder="平台图标名称" /></Form.Item>
    <Form.Item><Button type="primary" htmlType="submit" loading={busy}>{action}</Button></Form.Item>
  </Form>
}
