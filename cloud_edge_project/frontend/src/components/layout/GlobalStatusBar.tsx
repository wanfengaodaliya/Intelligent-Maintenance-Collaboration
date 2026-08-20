import { useMemo } from 'react';
import { Space, Tag, Switch, Select, Button, Tooltip, Typography } from 'antd';
import {
  ReloadOutlined,
  ClockCircleOutlined,
  AlertOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import type { ServiceStatusMap, ServiceName } from '../../types';
import { SERVICE_LABELS } from '../../utils/constants';

const { Text } = Typography;

const statusColors: Record<string, string> = {
  online: 'success',
  degraded: 'warning',
  offline: 'error',
  unknown: 'default',
};

interface GlobalStatusBarProps {
  currentTime: string;
  serviceStatus: ServiceStatusMap;
  pollingEnabled: boolean;
  pollingInterval: number;
  onTogglePolling: (enabled: boolean) => void;
  onChangeInterval: (ms: number) => void;
  onManualRefresh: () => void;
  lastUpdated: Date | null;
  isMockMode: boolean;
  dataSource: string;
}

export default function GlobalStatusBar({
  currentTime,
  serviceStatus,
  pollingEnabled,
  pollingInterval,
  onTogglePolling,
  onChangeInterval,
  onManualRefresh,
  lastUpdated,
  isMockMode,
  dataSource,
}: GlobalStatusBarProps) {
  const lastUpdatedText = useMemo(() => {
    if (!lastUpdated) return '暂无更新';
    return dayjs(lastUpdated).format('HH:mm:ss');
  }, [lastUpdated]);

  return (
    <div
      style={{
        height: 48,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '0 16px',
        background: '#fff',
        borderBottom: '1px solid var(--color-border-light)',
        gap: 12,
        flexWrap: 'nowrap',
        overflow: 'hidden',
      }}
    >
      <Space size={12} wrap={false}>
        <ClockCircleOutlined style={{ color: 'var(--color-text-secondary)' }} />
        <Text className="text-mono" style={{ fontSize: 13, fontWeight: 500 }}>
          {currentTime}
        </Text>
        <div style={{ width: 1, height: 20, background: 'var(--color-border-light)' }} />
        <Text style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>服务状态：</Text>
        {(Object.entries(serviceStatus) as [ServiceName, string][]).map(([name, status]) => (
          <Tooltip key={name} title={`${SERVICE_LABELS[name]}: ${status}`}>
            <Tag color={statusColors[status] || 'default'} style={{ margin: 0, fontSize: 11 }}>
              {SERVICE_LABELS[name]}
            </Tag>
          </Tooltip>
        ))}
      </Space>

      <Space size={8} wrap={false}>
        {isMockMode && (
          <Tag color="orange" style={{ margin: 0 }}>
            <AlertOutlined /> 演示数据
          </Tag>
        )}
        <Text style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
          来源: {dataSource}
        </Text>
        <div style={{ width: 1, height: 20, background: 'var(--color-border-light)' }} />
        <Text style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
          更新: {lastUpdatedText}
        </Text>
        <Tooltip title="自动刷新">
          <Switch
            size="small"
            checked={pollingEnabled}
            onChange={onTogglePolling}
            checkedChildren="开"
            unCheckedChildren="关"
          />
        </Tooltip>
        <Select
          size="small"
          value={pollingInterval}
          onChange={onChangeInterval}
          style={{ width: 80 }}
          options={[
            { value: 3000, label: '3s' },
            { value: 5000, label: '5s' },
            { value: 10000, label: '10s' },
            { value: 30000, label: '30s' },
          ]}
        />
        <Tooltip title="手动刷新">
          <Button
            size="small"
            icon={<ReloadOutlined />}
            onClick={onManualRefresh}
            type="text"
          />
        </Tooltip>
      </Space>
    </div>
  );
}