import { Alert, Button, Space } from 'antd';
import type { ApiError } from '../../types';

interface ErrorDisplayProps {
  error: ApiError | null;
  onRetry?: () => void;
  title?: string;
  style?: React.CSSProperties;
}

export default function ErrorDisplay({ error, onRetry, title, style }: ErrorDisplayProps) {
  if (!error) return null;

  const getMessage = () => {
    switch (error.error_code) {
      case 'NETWORK_ERROR':
        return '网络服务暂不可用，请确认服务端口已启动。';
      case 'REQUEST_TIMEOUT':
        return '请求超时，请检查网络连接或服务状态。';
      case 'EDGE_STATUS_NOT_FOUND':
        return '节点状态未找到，该节点可能尚未注册或已下线。';
      case 'UPDATE_NOT_FOUND':
        return '没有找到该更新任务，请检查 update_id。';
      case 'REVIEW_NOT_FOUND':
        return '没有找到该复核任务，请检查 review_id。';
      case 'SUMMARY_NOT_READY':
        return '复核结果尚未生成，请稍后重试。';
      case 'LEGACY_BEARING_WORKFLOW_DISABLED':
        return '当前云端配置未启用传统轴承复核工作流。';
      default:
        return error.message || error.detail || `请求失败 (${error.error_code})`;
    }
  };

  return (
    <Alert
      type="error"
      showIcon
      message={title || '请求错误'}
      description={
        <Space direction="vertical" size={4}>
          <span>{getMessage()}</span>
          {error.error_code && (
            <code style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
              错误码: {error.error_code}
            </code>
          )}
        </Space>
      }
      action={
        onRetry ? (
          <Button size="small" onClick={onRetry}>
            重试
          </Button>
        ) : undefined
      }
      style={style}
    />
  );
}