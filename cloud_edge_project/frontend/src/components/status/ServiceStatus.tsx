import { Tag, Tooltip } from 'antd';
import type { ServiceName } from '../../types';
import { SERVICE_LABELS } from '../../utils/constants';

interface ServiceStatusProps {
  name: ServiceName;
  status: string;
  showLabel?: boolean;
}

const statusColors: Record<string, string> = {
  online: 'success',
  degraded: 'warning',
  offline: 'error',
  unknown: 'default',
};

export default function ServiceStatus({
  name,
  status,
  showLabel = true,
}: ServiceStatusProps) {
  return (
    <Tooltip title={`${SERVICE_LABELS[name]}: ${status}`}>
      <Tag color={statusColors[status] || 'default'}>
        {showLabel ? SERVICE_LABELS[name] : status}
      </Tag>
    </Tooltip>
  );
}