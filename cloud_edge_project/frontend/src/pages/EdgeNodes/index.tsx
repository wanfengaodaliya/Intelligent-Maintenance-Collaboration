import { useState } from 'react';
import { Card, Row, Col, Tag, Typography, Statistic, Drawer, Descriptions, Space, Progress, Alert, Table, Button } from 'antd';
import { ReloadOutlined, WarningOutlined } from '@ant-design/icons';
import { useEdgeNodes } from '../../hooks/useEdgeNodes';
import { formatBytes } from '../../utils/format';
import { formatNsTimestamp, fromNsAgo } from '../../utils/time';
import LoadingSkeleton from '../../components/feedback/LoadingSkeleton';
import ErrorDisplay from '../../components/feedback/ErrorDisplay';
import type { EdgeNodeWithStatus } from '../../hooks/useEdgeNodes';
import type { NodeOnlineStatus } from '../../types';

const { Title, Text } = Typography;

const STATUS_CONFIG: Record<NodeOnlineStatus, { color: string; label: string }> = {
  online: { color: 'success', label: '在线' },
  stale: { color: 'warning', label: '陈旧' },
  offline: { color: 'error', label: '离线' },
  unknown: { color: 'default', label: '未知' },
};

const LOAD_STATUS_COLORS: Record<string, string> = {
  LOADED: 'success',
  LOADING: 'processing',
  UNLOADED: 'default',
  ERROR: 'error',
};

export default function EdgeNodesPage() {
  const { data: nodes, isLoading, error, refetch } = useEdgeNodes();
  const [selectedNode, setSelectedNode] = useState<EdgeNodeWithStatus | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  if (isLoading) return <LoadingSkeleton type="card" count={2} />;

  if (error) {
    return <ErrorDisplay error={error} onRetry={() => refetch()} title="边缘节点加载失败" />;
  }

  if (!nodes || nodes.length === 0) {
    return (
      <div style={{ padding: 24 }}>
        <ErrorDisplay
          error={{ error_code: 'NO_NODES', message: '没有获取到边缘节点状态。请确认 VITE_EDGE_NODE_IDS 配置正确，并确保边缘服务已启动。' }}
          onRetry={() => refetch()}
        />
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <Title level={4} style={{ margin: 0 }}>边缘节点</Title>
        <Button icon={<ReloadOutlined />} onClick={() => refetch()} size="small">刷新</Button>
      </div>

      {/* Node Cards */}
      <Row gutter={[16, 16]}>
        {nodes.map(node => {
          const statusConfig = STATUS_CONFIG[node.onlineStatus];
          const cpuPercent = node.resources.cpu_utilization_percent;
          const memPercent = node.resources.memory_available_mb > 0
            ? Math.min(100, 100 - (node.resources.memory_available_mb / 8192) * 100)
            : 0;
          const modelVersions = node.models.map(m => m.model_version).join(', ');

          return (
            <Col xs={24} lg={12} key={node.edge_node_id}>
              <Card
                hoverable
                onClick={() => { setSelectedNode(node); setDrawerOpen(true); }}
                title={
                  <Space>
                    <code>{node.edge_node_id}</code>
                    <Tag color={statusConfig.color}>{statusConfig.label}</Tag>
                  </Space>
                }
                size="small"
              >
                <Row gutter={[16, 16]}>
                  <Col span={12}>
                    <Statistic title="CPU" value={`${cpuPercent.toFixed(1)}%`} valueStyle={{ fontSize: 18, fontFamily: 'monospace' }} />
                    <Progress percent={Math.round(cpuPercent)} size="small" status={cpuPercent > 80 ? 'exception' : cpuPercent > 60 ? 'active' : 'normal'} />
                  </Col>
                  <Col span={12}>
                    <Statistic title="可用内存" value={formatBytes(node.resources.memory_available_mb * 1024 * 1024)} valueStyle={{ fontSize: 18, fontFamily: 'monospace' }} />
                    <Progress percent={Math.round(memPercent)} size="small" status={memPercent > 80 ? 'exception' : 'normal'} />
                  </Col>
                </Row>
                <Row gutter={[16, 8]} style={{ marginTop: 12 }}>
                  <Col span={8}>
                    <Text style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>队列</Text>
                    <div style={{ fontFamily: 'monospace', fontSize: 14 }}>{node.resources.queue_length}</div>
                  </Col>
                  <Col span={8}>
                    <Text style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>GPU/NPU</Text>
                    <div style={{ fontSize: 14 }}>
                      <Tag color={node.resources.gpu_available ? 'success' : 'default'} style={{ fontSize: 10 }}>GPU</Tag>
                      <Tag color={node.resources.npu_available ? 'success' : 'default'} style={{ fontSize: 10 }}>NPU</Tag>
                    </div>
                  </Col>
                  <Col span={8}>
                    <Text style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>模型版本</Text>
                    <div style={{ fontFamily: 'monospace', fontSize: 12 }}>{modelVersions || '无'}</div>
                  </Col>
                </Row>
                {node.models.some(m => m.load_status !== 'LOADED') && (
                  <Alert
                    type="warning"
                    showIcon
                    icon={<WarningOutlined />}
                    message={
                      <Text style={{ fontSize: 12 }}>
                        {node.models.filter(m => m.load_status !== 'LOADED').map(m => `${m.model_version}: ${m.load_status}`).join('; ')}
                      </Text>
                    }
                    style={{ marginTop: 8 }}
                  />
                )}
                <div style={{ marginTop: 8, fontSize: 11, color: 'var(--color-text-secondary)' }}>
                  上次上报: {node.reported_at_ns ? fromNsAgo(node.reported_at_ns) : '未知'}
                  {node.edgeHealth && (
                    <span style={{ marginLeft: 8 }}>
                      | 边缘服务: <Tag color={node.edgeHealth.status === 'ok' ? 'success' : 'error'} style={{ fontSize: 10 }}>{node.edgeHealth.status}</Tag>
                    </span>
                  )}
                </div>
              </Card>
            </Col>
          );
        })}
      </Row>

      {/* Node Comparison Table */}
      {nodes.length > 1 && (
        <Card title="多节点对比" size="small" style={{ marginTop: 16 }}>
          <Table
            dataSource={nodes}
            rowKey="edge_node_id"
            size="small"
            pagination={false}
            columns={[
              { title: '节点', dataIndex: 'edge_node_id', key: 'edge_node_id', render: (v: string) => <code>{v}</code> },
              { title: '状态', dataIndex: 'onlineStatus', key: 'onlineStatus', render: (v: NodeOnlineStatus) => <Tag color={STATUS_CONFIG[v].color}>{STATUS_CONFIG[v].label}</Tag> },
              { title: 'CPU', dataIndex: ['resources', 'cpu_utilization_percent'], key: 'cpu', render: (v: number) => `${v.toFixed(1)}%` },
              { title: '可用内存', dataIndex: ['resources', 'memory_available_mb'], key: 'memory', render: (v: number) => formatBytes(v * 1024 * 1024) },
              { title: '队列', dataIndex: ['resources', 'queue_length'], key: 'queue' },
              { title: 'GPU', dataIndex: ['resources', 'gpu_available'], key: 'gpu', render: (v: boolean) => <Tag color={v ? 'success' : 'default'}>{v ? '可用' : '无'}</Tag> },
              { title: 'NPU', dataIndex: ['resources', 'npu_available'], key: 'npu', render: (v: boolean) => <Tag color={v ? 'success' : 'default'}>{v ? '可用' : '无'}</Tag> },
              { title: '模型版本', key: 'models', render: (_: unknown, record: EdgeNodeWithStatus) => record.models.map(m => m.model_version).join(', ') },
              { title: 'RTT', dataIndex: ['network_to_scheduler', 'rtt_ms_avg'], key: 'rtt', render: (v: number | undefined) => v !== undefined ? `${v.toFixed(1)}ms` : '-' },
            ]}
          />
        </Card>
      )}

      {/* Node Detail Drawer */}
      <Drawer
        title={`节点详情: ${selectedNode?.edge_node_id}`}
        placement="right"
        width={520}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
      >
        {selectedNode && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions title="节点信息" column={2} size="small" bordered>
              <Descriptions.Item label="节点ID"><code>{selectedNode.edge_node_id}</code></Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag color={STATUS_CONFIG[selectedNode.onlineStatus].color}>
                  {STATUS_CONFIG[selectedNode.onlineStatus].label}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="最后上报">{selectedNode.reported_at_ns ? formatNsTimestamp(selectedNode.reported_at_ns) : '未知'}</Descriptions.Item>
              <Descriptions.Item label="最后活动">{selectedNode.last_task_activity_ns ? fromNsAgo(selectedNode.last_task_activity_ns) : '未知'}</Descriptions.Item>
            </Descriptions>

            <Descriptions title="资源" column={2} size="small" bordered>
              <Descriptions.Item label="逻辑 CPU">{selectedNode.resources.logical_cpu_count}</Descriptions.Item>
              <Descriptions.Item label="CPU 利用率">{selectedNode.resources.cpu_utilization_percent.toFixed(1)}%</Descriptions.Item>
              <Descriptions.Item label="可用内存">{formatBytes(selectedNode.resources.memory_available_mb * 1024 * 1024)}</Descriptions.Item>
              <Descriptions.Item label="队列长度">{selectedNode.resources.queue_length}</Descriptions.Item>
              <Descriptions.Item label="GPU"><Tag color={selectedNode.resources.gpu_available ? 'success' : 'default'}>{selectedNode.resources.gpu_available ? '可用' : '不可用'}</Tag></Descriptions.Item>
              <Descriptions.Item label="NPU"><Tag color={selectedNode.resources.npu_available ? 'success' : 'default'}>{selectedNode.resources.npu_available ? '可用' : '不可用'}</Tag></Descriptions.Item>
            </Descriptions>

            <Descriptions title="模型" column={1} size="small" bordered>
              {selectedNode.models.map((m, i) => (
                <Descriptions.Item label={`模型 ${i + 1}`} key={i}>
                  <Space>
                    <code>{m.model_version}</code>
                    <Tag color={LOAD_STATUS_COLORS[m.load_status] || 'default'}>{m.load_status}</Tag>
                  </Space>
                </Descriptions.Item>
              ))}
              {selectedNode.models.length === 0 && (
                <Descriptions.Item label="模型">无模型信息</Descriptions.Item>
              )}
            </Descriptions>

            {selectedNode.network_to_scheduler && (
              <Descriptions title="边缘到调度器网络" column={2} size="small" bordered>
                <Descriptions.Item label="测量时间">{formatNsTimestamp(selectedNode.network_to_scheduler.measured_at_ns)}</Descriptions.Item>
                <Descriptions.Item label="可用上行">{selectedNode.network_to_scheduler.available_uplink_mbps_estimate.toFixed(1)} Mbps</Descriptions.Item>
                <Descriptions.Item label="RTT 平均">{selectedNode.network_to_scheduler.rtt_ms_avg.toFixed(1)} ms</Descriptions.Item>
                <Descriptions.Item label="RTT P95">{selectedNode.network_to_scheduler.rtt_ms_p95.toFixed(1)} ms</Descriptions.Item>
                <Descriptions.Item label="丢包率">{(selectedNode.network_to_scheduler.loss_rate * 100).toFixed(2)}%</Descriptions.Item>
              </Descriptions>
            )}

            {selectedNode.edgeHealth && (
              <Descriptions title="边缘服务健康" column={2} size="small" bordered>
                <Descriptions.Item label="服务状态">{selectedNode.edgeHealth.status}</Descriptions.Item>
                <Descriptions.Item label="模型版本">{selectedNode.edgeHealth.model_version || '-'}</Descriptions.Item>
                <Descriptions.Item label="部署状态">{selectedNode.edgeHealth.model_deployment_status || '-'}</Descriptions.Item>
                <Descriptions.Item label="MQTT 连接">{selectedNode.edgeHealth.mqtt_connected ? <Tag color="success">已连接</Tag> : <Tag color="error">未连接</Tag>}</Descriptions.Item>
              </Descriptions>
            )}
          </Space>
        )}
      </Drawer>
    </div>
  );
}