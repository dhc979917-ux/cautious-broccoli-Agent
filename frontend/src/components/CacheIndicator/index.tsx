import { FC } from 'react';
import { Tooltip } from 'antd';
import type { TokenUsage } from '../../pages/Chat/types';
import TokenUsageTooltip from '../TokenUsageTooltip';
import './index.css';

interface CacheIndicatorProps {
  usage: TokenUsage | null;
}

const CacheIndicator: FC<CacheIndicatorProps> = ({ usage }) => {
  const rate = usage?.cache_rate;
  const label = rate == null ? '—' : `${rate.toFixed(1)}%`;

  return (
    <Tooltip title={<TokenUsageTooltip usage={usage} />} overlayClassName="usage-tooltip-overlay" trigger={['hover', 'focus']}>
      <span className="cache-indicator" tabIndex={0} aria-label={`本会话缓存命中率 ${label}，查看累计用量详情`}>
        <span className="cache-dot" />
        <span className="cache-label">缓存命中率</span>
        <span className="cache-rate">{label}</span>
      </span>
    </Tooltip>
  );
};

export default CacheIndicator;
