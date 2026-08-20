import { useState } from 'react';
import { Card, Table, Tag, Button, Typography, Row, Col, Statistic, Drawer, Descriptions, Space, Segmented, Alert, Tooltip } from 'antd';
import { ReloadOutlined, ApartmentOutlined, UnorderedListOutlined } from '@ant-design/icons';
import { useNetworkLinks, useNetworkRuntime, useNetworkHealth } from '../../hooks/useNetwork';
import { formatMs, formatKbps, formatScore } from '../../utils/format';
import { fromNsAgo } from '../../utils/time';
import LoadingSkeleton from '../../components/feedback/LoadingSkeleton';
import ErrorDisplay from '../../components/feedback/ErrorDisplay';
import type { NetworkLink } from '../../types';

const { Title, Text } = Typography;

const STATE_COLORS: Record<string, string> = {
  normal: 'success',
  degraded: 'warning',
  disrupted: 'error',
  flapping: 'warning',
};

const LINK_TYPE_LABELS: Record<string, string> = {
  sender_to_edge: '发送端→边缘',
  edge_to_scheduler: '边缘→调度器',
  scheduler_to_cloud: '调度器→云端',
};

function LinkStateTag({ state }: { state: string }) {
  const color = STATE_COLORS[state] || 'default';
  const label = state === 'normal' ? '正常' : state === 'degraded' ? '降级' : state === 'disrupted' ? '中断' : state === 'flapping' ? '抖动' : state;
  return <Tag color={color}>{label}</Tag>;
}

export default function NetworkPage() {
  const { data: links, isLoading: linksLoading, error: linksError, refetch: refetchLinks } = useNetworkLinks();
  const { data: runtime } = useNetworkRuntime();
  const { data: health } = useNetworkHealth();
  const [viewMode, setViewMode] = useState<'topology' | 'table'>('table');
  const [selectedLink, setSelectedLink] = useState<NetworkLink | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const columns = [
    { title: '链路ID', dataIndex: 'link_id', key: 'link_id', width: 200, render: (v: string) => <code>{v}</code> },
    { title: '类型', dataIndex: 'link_type', key: 'link_type', width: 120, render: (v: string) => LINK_TYPE_LABELS[v] || v },
    { title: '发送端', dataIndex: 'sender_id', key: 'sender_id', width: 100, render: (v: string | null) => v || '-' },
    { title: '边缘节点', dataIndex: 'edge_id', key: 'edge_id', width: 100, render: (v: string | null) => v || '-' },
    { title: '协议', dataIndex: 'protocol', key: 'protocol', width: 70 },
    { title: '状态', dataIndex: 'current_state', key: 'current_state', width: 80, render: (v: string) => <LinkStateTag state={v} /> },
    { title: '延迟(ms)', dataIndex: 'latency_ms', key: 'latency_ms', width: 80, render: (v: number | null) => v !== null && v !== undefined ? formatMs(v) : <Text type="secondary">暂无测量</Text> },
    { title: '抖动(ms)', dataIndex: 'jitter_ms', key: 'jitter_ms', width: 80, render: (v: number | null) => v !== null && v !== undefined ? formatMs(v) : <Text type="secondary">暂无测量</Text> },
    { title: '带宽(Kbps)', dataIndex: 'bandwidth_kbps', key: 'bandwidth_kbps', width: 100, render: (v: number | null) => v !== null && v !== undefined ? formatKbps(v) : <Text type="secondary">暂无测量</Text> },
    { title: '丢包率(%)', dataIndex: 'packet_loss_percent', key: 'packet_loss_percent', width: 90, render: (v: number | null, record: NetworkLink) => {
      const applied = record.applied_parameters;
      if (applied && !applied.packet_loss_applied) {
        return <Tooltip title="丢包率为模型参数，当前未真实施加"><Text type="warning">{v !== null ? `${v.toFixed(1)}%` : '暂无测量'}</Text></Tooltip>;
      }
      return v !== null && v !== undefined ? `${v.toFixed(1)}%` : <Text type="secondary">暂无测量</Text>;
    }},
    { title: '可用', dataIndex: 'available', key: 'available', width: 70, render: (v: boolean) => <Tag color={v ? 'success' : 'error'}>{v ? '是' : '否'}</Tag> },
    { title: '操作', key: 'action', width: 70, render: (_: unknown, record: NetworkLink) => (
      <Button type="link" size="small" onClick={() => { setSelectedLink(record); setDrawerOpen(true); }}>详情</Button>
    )},
  ];

  const availableCount = links?.filter(l => l.available).length || 0;

  if (linksLoading) return <LoadingSkeleton type="table" />;

  if (linksError) {
    return (
      <div style={{ padding: 24 }}>
        <ErrorDisplay error={linksError} onRetry={() => refetchLinks()} title="网络链路加载失败" />
        <Alert
          type="info"
          showIcon
          message="网络模拟器启动提示"
          description="请确认 Docker Desktop 已启动，并按 network_simulator/README.md 运行网络模拟器。默认端口: 8090。"
          style={{ marginTop: 16 }}
        />
      </div>
    );
  }

  return (
    <div>
      <Title level={4} style={{ marginBottom: 16 }}>网络链路</Title>

      {/* Stats + Controls */}
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} lg={4}><Card size="small"><Statistic title="链路总数" value={links?.length || 0} /></Card></Col>
        <Col xs={12} lg={4}><Card size="small"><Statistic title="可用链路" value={availableCount} valueStyle={{ color: 'var(--color-accent-green)' }} /></Card></Col>
        <Col xs={12} lg={4}><Card size="small"><Statistic title="当前 Tick" value={runtime?.tick || 0} /></Card></Col>
        <Col xs={12} lg={4}><Card size="small"><Statistic title="Toxiproxy" value={health?.toxiproxy_available ? '已连接' : '未连接'} valueStyle={{ color: health?.toxiproxy_available ? 'var(--color-accent-green)' : 'var(--color-accent-red)' }} /></Card></Col>
        <Col xs={24} lg={8}>
          <Card size="small">
            <Space>
              <Segmented
                value={viewMode}
                onChange={(v) => setViewMode(v as 'topology' | 'table')}
                options={[
                  { value: 'table', icon: <UnorderedListOutlined />, label: '表格' },
                  { value: 'topology', icon: <ApartmentOutlined />, label: '拓扑' },
                ]}
              />
              <Button icon={<ReloadOutlined />} onClick={() => refetchLinks()} size="small">刷新</Button>
            </Space>
          </Card>
        </Col>
      </Row>

      {viewMode === 'table' ? (
        <Card size="small" bodyStyle={{ padding: 0 }}>
          {links && links.length > 0 ? (
            <Table
              dataSource={links}
              columns={columns}
              rowKey="link_id"
              size="small"
              pagination={false}
              scroll={{ x: 1100 }}
              onRow={(record) => ({
                style: { cursor: 'pointer' },
                onClick: () => { setSelectedLink(record); setDrawerOpen(true); },
              })}
            />
          ) : (
            <div style={{ padding: 48, textAlign: 'center', color: 'var(--color-text-secondary)' }}>暂无链路数据</div>
          )}
        </Card>
      ) : (
        /* Topology View */
        <Card title="网络拓扑视图" size="small">
          {links && links.length > 0 ? (
            <div style={{ padding: '24px 0', textAlign: 'center' }}>
              <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 24, flexWrap: 'wrap' }}>
                {links.map(link => (
                  <Card
                    key={link.link_id}
                    size="small"
                    hoverable
                    onClick={() => { setSelectedLink(link); setDrawerOpen(true); }}
                    style={{ width: 200, textAlign: 'center', border: `2px solid ${link.available ? 'var(--color-accent-green)' : 'var(--color-accent-red)'}` }}
                  >
                    <div style={{ fontWeight: 600, marginBottom: 4 }}>{LINK_TYPE_LABELS[link.link_type] || link.link_type}</div>
                    <code style={{ fontSize: 11 }}>{link.link_id}</code>
                    <div style={{ marginTop: 8 }}><LinkStateTag state={link.current_state} /></div>
                    <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', marginTop: 4 }}>
                      {link.sender_id || '?'} → {link.edge_id || '?'}
                    </div>
                    <div style={{ fontSize: 11, marginTop: 4 }}>
                      {link.latency_ms !== null ? `${link.latency_ms}ms` : '暂无测量'} | {link.bandwidth_kbps !== null ? `${link.bandwidth_kbps}Kbps` : '暂无测量'}
                    </div>
                  </Card>
                ))}
              </div>
            </div>
          ) : (
            <div style={{ padding: 48, textAlign: 'center', color: 'var(--color-text-secondary)' }}>暂无链路数据</div>
          )}
        </Card>
      )}

      {/* Link Detail Drawer */}
      <Drawer
        title={`链路详情: ${selectedLink?.link_id}`}
        placement="right"
        width={520}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
      >
        {selectedLink && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions title="基本信息" column={2} size="small" bordered>
              <Descriptions.Item label="链路ID" span={2}><code>{selectedLink.link_id}</code></Descriptions.Item>
              <Descriptions.Item label="类型">{LINK_TYPE_LABELS[selectedLink.link_type] || selectedLink.link_type}</Descriptions.Item>
              <Descriptions.Item label="协议">{selectedLink.protocol}</Descriptions.Item>
              <Descriptions.Item label="发送端">{selectedLink.sender_id || '-'}</Descriptions.Item>
              <Descriptions.Item label="边缘节点">{selectedLink.edge_id || '-'}</Descriptions.Item>
              <Descriptions.Item label="监听地址">{selectedLink.listen}</Descriptions.Item>
              <Descriptions.Item label="上游地址">{selectedLink.upstream}</Descriptions.Item>
            </Descriptions>

            <Descriptions title="实时状态" column={2} size="small" bordered>
              <Descriptions.Item label="当前状态"><LinkStateTag state={selectedLink.current_state} /></Descriptions.Item>
              <Descriptions.Item label="上一状态">{selectedLink.previous_state}</Descriptions.Item>
              <Descriptions.Item label="状态持续">{fromNsAgo(selectedLink.state_since_ns)}</Descriptions.Item>
              <Descriptions.Item label="可用"><Tag color={selectedLink.available ? 'success' : 'error'}>{selectedLink.available ? '是' : '否'}</Tag></Descriptions.Item>
              <Descriptions.Item label="延迟">{selectedLink.latency_ms !== null ? formatMs(selectedLink.latency_ms) : <Text type="secondary">暂无测量</Text>}</Descriptions.Item>
              <Descriptions.Item label="抖动">{selectedLink.jitter_ms !== null ? formatMs(selectedLink.jitter_ms) : <Text type="secondary">暂无测量</Text>}</Descriptions.Item>
              <Descriptions.Item label="带宽">{selectedLink.bandwidth_kbps !== null ? formatKbps(selectedLink.bandwidth_kbps) : <Text type="secondary">暂无测量</Text>}</Descriptions.Item>
              <Descriptions.Item label="链路可靠度">{formatScore(selectedLink.link_reliability_score)}</Descriptions.Item>
              <Descriptions.Item label="连续失败">{selectedLink.consecutive_apply_failures}</Descriptions.Item>
              <Descriptions.Item label="上次应用成功"><Tag color={selectedLink.last_apply_success ? 'success' : 'error'}>{selectedLink.last_apply_success ? '是' : '否'}</Tag></Descriptions.Item>
            </Descriptions>

            {selectedLink.desired_parameters && (
              <Descriptions title="期望参数" column={2} size="small" bordered>
                <Descriptions.Item label="延迟">{selectedLink.desired_parameters.latency_ms !== null ? `${selectedLink.desired_parameters.latency_ms}ms` : '无'}</Descriptions.Item>
                <Descriptions.Item label="抖动">{selectedLink.desired_parameters.jitter_ms !== null ? `${selectedLink.desired_parameters.jitter_ms}ms` : '无'}</Descriptions.Item>
                <Descriptions.Item label="带宽">{selectedLink.desired_parameters.bandwidth_kbps !== null ? `${selectedLink.desired_parameters.bandwidth_kbps}Kbps` : '无'}</Descriptions.Item>
                <Descriptions.Item label="丢包率">{selectedLink.desired_parameters.packet_loss_percent}%</Descriptions.Item>
                <Descriptions.Item label="丢包真实施加" span={2}>
                  <Tag color={selectedLink.desired_parameters.packet_loss_applied ? 'success' : 'warning'}>
                    {selectedLink.desired_parameters.packet_loss_applied ? '是' : '否（模型参数，未真实施加）'}
                  </Tag>
                </Descriptions.Item>
              </Descriptions>
            )}

            {selectedLink.error && (
              <Alert type="error" showIcon message="错误信息" description={selectedLink.error} />
            )}
          </Space>
        )}
      </Drawer>
    </div>
  );
}