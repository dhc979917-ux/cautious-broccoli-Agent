import type { TokenUsage } from '../../pages/Chat/types'
import { formatTokenCount, getUncachedInputTokens } from '../../utils/tokenUsage'
import './index.css'

export default function TokenUsageTooltip({ usage }: { usage?: TokenUsage | null }) {
  return (
    <div className="token-usage-breakdown">
      <div><span>输入（未命中）：</span><strong>{formatTokenCount(getUncachedInputTokens(usage))}</strong></div>
      <div><span>输出：</span><strong>{formatTokenCount(usage?.output_tokens)}</strong></div>
      <div><span>缓存命中输入：</span><strong>{formatTokenCount(usage?.cache_input_tokens)}</strong></div>
    </div>
  )
}
