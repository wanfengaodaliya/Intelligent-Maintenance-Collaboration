import { Empty, Button } from 'antd';

interface EmptyStateProps {
  description?: string;
  onAction?: () => void;
  actionText?: string;
}

export default function EmptyState({
  description = '暂无数据',
  onAction,
  actionText = '刷新',
}: EmptyStateProps) {
  return (
    <div style={{ padding: '48px 0', textAlign: 'center' }}>
      <Empty description={description}>
        {onAction && (
          <Button type="primary" onClick={onAction} size="small">
            {actionText}
          </Button>
        )}
      </Empty>
    </div>
  );
}