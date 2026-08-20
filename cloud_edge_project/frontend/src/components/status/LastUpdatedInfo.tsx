import { Typography } from 'antd';
import dayjs from 'dayjs';

const { Text } = Typography;

interface LastUpdatedInfoProps {
  lastUpdated: Date | null;
  isStale: boolean;
  staleThresholdMs: number;
}

export default function LastUpdatedInfo({
  lastUpdated,
  isStale,
  staleThresholdMs,
}: LastUpdatedInfoProps) {
  if (!lastUpdated) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }}>
        暂无更新数据
      </Text>
    );
  }

  const ago = dayjs(lastUpdated).fromNow();

  return (
    <div>
      <Text style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
        最后更新: {dayjs(lastUpdated).format('HH:mm:ss')} ({ago})
      </Text>
      {isStale && (
        <Text type="warning" style={{ fontSize: 12, marginLeft: 8, display: 'block' }}>
          数据已超过 {staleThresholdMs / 1000} 秒未更新，可能已失效
        </Text>
      )}
    </div>
  );
}