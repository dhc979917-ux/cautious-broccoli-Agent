import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export default function MemoryMarkdown({ content }: { content: string }) {
  return <div className="memory-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{
    a: ({ children, href }) => href && /^https?:\/\//i.test(href) ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
    img: ({ alt }) => <span className="memory-image-label">{alt ? `图片：${alt}` : '图片内容'}</span>,
  }}>{content}</ReactMarkdown></div>
}
