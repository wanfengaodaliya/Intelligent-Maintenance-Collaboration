import { useState, useMemo } from 'react';
import { Card, Table, Input, Select, Tag, Button, Typography, Row, Col, Statistic, Space, Drawer, Timeline, Descriptions, Alert } from 'antd';
import { SearchOutlined, FilterOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { useTasks } from '../../hooks/useTasks';
import { formatMs, formatRoute, formatLabel, formatRiskLevel } from '../../utils/format';
import { fromNow } from '../../utils/time';
import { getDeviceRecommendation } from '../../adapters/taskAdapter';
import LoadingSkeleton from '../../components/feedback/LoadingSkeleton';
import ErrorDisplay from '../../components/feedback/ErrorDisplay';
import EmptyState from '../../components/feedback/EmptyState';
import type { UnifiedTask } from '../../types';

const { Title, Text } = Typography;

const RISK_COLORS: Record<string, string> = {
  low: 'green',
  medium: 'orange',
  high: 'red',
  critical: 'red',
};

const ROUTE_OPTIONS = [
  { value: '', label: '全部路线' },
  { value: 'edge', label: '边缘处理' },
  { value: 'cloud', label: '云端复核' },
  { value: 'edge_cloud', label: '边云协同' },
  { value: 'fallback_edge', label: '边缘降级' },
];

const RISK_OPTIONS = [
  { value: '', label: '全部风险' },
  { value: 'low', label: '低风险' },
  { value: 'medium', label: '中风险' },
  { value: 'high', label: '高风险' },
  { value: 'critical', label: '严重风险' },
];

export default function TasksPage() {
  const { data: tasks, isLoading, error, refetch } = useTasks(100);
  
  const [searchText, setSearchText] = useState('');
  const [routeFilter, setRouteFilter] = useState('');
  const [riskFilter, setRiskFilter] = useState('');
  const [successFilter, setSuccessFilter] = useState<string>('');
  const [selectedTask, setSelectedTask] = useState<UnifiedTask | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const filteredTasks = useMemo(() => {
    if (!tasks) return [];
    return tasks.filter(task => {
      if (searchText && !task.id.toLowerCase().includes(searchText.toLowerCase()) && !task.source.toLowerCase().includes(searchText.toLowerCase())) return false;
      if (routeFilter && task.route !== routeFilter) return false;
      if (riskFilter && task.risk_level !== riskFilter) return false;
      if (successFilter === 'success' && !task.success) return false;
      if (successFilter === 'fail' && task.success) return false;
      return true;
    });
  }, [tasks, searchText, routeFilter, riskFilter, successFilter]);

  const stats = useMemo(() => {
    if (!tasks) return { total: 0, success: 0, fail: 0, abnormal: 0 };
    return {
      total: tasks.length,
      success: tasks.filter(t => t.success).length,
      fail: tasks.filter(t => !t.success).length,
      abnormal: tasks.filter(t => t.label === 'abnormal').length,
    };
  }, [tasks]);

  const columns = [
    { title: '任务ID', dataIndex: 'id', key: 'id', width: 140, render: (v: string) => <code>{v}</code> },
    { title: '来源', dataIndex: 'source', key: 'source', width: 100 },
    { title: '路线', dataIndex: 'route', key: 'route', width: 100, render: (v: string) => formatRoute(v) },
    { title: '结论', dataIndex: 'label', key: 'label', width: 80, render: (v: string | undefined) => v ? formatLabel(v) : <Text type="secondary">未知</Text> },
    { title: '置信度', dataIndex: 'confidence', key: 'confidence', width: 90, render: (v: number | undefined) => v ? `${(v * 100).toFixed(1)}%` : '-' },
    { title: '风险等级', dataIndex: 'risk_level', key: 'risk_level', width: 90, render: (v: string | undefined) => v ? <Tag color={RISK_COLORS[v] || 'default'}>{formatRiskLevel(v)}</Tag> : '-' },
    { title: '延迟', dataIndex: 'total_latency_ms', key: 'total_latency_ms', width: 80, render: (v: number) => formatMs(v), sorter: (a: UnifiedTask, b: UnifiedTask) => a.total_latency_ms - b.total_latency_ms },
    { title: '状态', dataIndex: 'success', key: 'success', width: 70, render: (v: boolean) => <Tag color={v ? 'success' : 'error'}>{v ? '成功' : '失败'}</Tag> },
    { title: '时间', dataIndex: 'timestamp', key: 'timestamp', width: 120, render: (v: string) => fromNow(v) },
    {
      title: '操作', key: 'action', width: 80, render: (_: unknown, record: UnifiedTask) => (
        <Button type="link" size="small" onClick={() => { setSelectedTask(record); setDrawerOpen(true); }}>详情</Button>
      ),
    },
  ];

  if (isLoading) return <LoadingSkeleton type="table" />;

  if (error) {
    return <ErrorDisplay error={error} onRetry={() => refetch()} title="任务列表加载失败" />;
  }

  return (
    <div>
      <Title level={4} style={{ marginBottom: 16 }}>任务结果与设备建议</Title>

      {/* Stats */}
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={8} lg={4}><Card size="small"><Statistic title="总任务" value={stats.total} /></Card></Col>
        <Col xs={8} lg={4}><Card size="small"><Statistic title="成功" value={stats.success} valueStyle={{ color: 'var(--color-accent-green)' }} /></Card></Col>
        <Col xs={8} lg={4}><Card size="small"><Statistic title="失败" value={stats.fail} valueStyle={{ color: 'var(--color-accent-red)' }} /></Card></Col>
        <Col xs={8} lg={4}><Card size="small"><Statistic title="异常" value={stats.abnormal} valueStyle={{ color: stats.abnormal > 0 ? 'var(--color-accent-red)' : undefined }} /></Card></Col>
        {tasks && tasks.length > 0 && (
          <Col xs={24} lg={8}>
            <Card size="small">
              <Text style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
                最近更新: {fromNow(tasks[0].timestamp)}
              </Text>
            </Card>
          </Col>
        )}
      </Row>

      {/* Filters */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap>
          <Input
            placeholder="搜索任务ID或设备..."
            prefix={<SearchOutlined />}
            value={searchText}
            onChange={e => setSearchText(e.target.value)}
            style={{ width: 200 }}
            allowClear
          />
          <Select value={routeFilter} onChange={setRouteFilter} options={ROUTE_OPTIONS} style={{ width: 120 }} />
          <Select value={riskFilter} onChange={setRiskFilter} options={RISK_OPTIONS} style={{ width: 120 }} />
          <Select
            value={successFilter}
            onChange={setSuccessFilter}
            options={[
              { value: '', label: '全部状态' },
              { value: 'success', label: '成功' },
              { value: 'fail', label: '失败' },
            ]}
            style={{ width: 120 }}
          />
          <Button icon={<FilterOutlined />} onClick={() => { setSearchText(''); setRouteFilter(''); setRiskFilter(''); setSuccessFilter(''); }}>
            重置
          </Button>
        </Space>
      </Card>

      {/* Task Table */}
      <Card size="small" bodyStyle={{ padding: 0 }}>
        {filteredTasks.length > 0 ? (
          <Table
            dataSource={filteredTasks}
            columns={columns}
            rowKey="id"
            size="small"
            pagination={{ pageSize: 20, showSizeChanger: true, showTotal: (t: number) => `共 ${t} 条` }}
            scroll={{ x: 900 }}
            onRow={(record) => ({
              style: { cursor: 'pointer' },
              onClick: () => { setSelectedTask(record); setDrawerOpen(true); },
            })}
          />
        ) : tasks && tasks.length > 0 ? (
          <EmptyState description="没有匹配的任务" onAction={() => { setSearchText(''); setRouteFilter(''); setRiskFilter(''); setSuccessFilter(''); }} actionText="清除筛选" />
        ) : (
          <EmptyState description="暂无任务数据" onAction={() => refetch()} />
        )}
      </Card>

      {/* Task Detail Drawer */}
      <Drawer
        title={`任务详情: ${selectedTask?.id}`}
        placement="right"
        width={480}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
      >
        {selectedTask && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions title="基本信息" column={2} size="small" bordered>
              <Descriptions.Item label="任务ID"><code>{selectedTask.id}</code></Descriptions.Item>
              <Descriptions.Item label="来源">{selectedTask.source}</Descriptions.Item>
              <Descriptions.Item label="路线">{formatRoute(selectedTask.route)}</Descriptions.Item>
              <Descriptions.Item label="场景">{selectedTask.scenario || 'bearing'}</Descriptions.Item>
              <Descriptions.Item label="结论">{selectedTask.label ? formatLabel(selectedTask.label) : '未知'}</Descriptions.Item>
              <Descriptions.Item label="置信度">{selectedTask.confidence ? `${(selectedTask.confidence * 100).toFixed(1)}%` : '-'}</Descriptions.Item>
              <Descriptions.Item label="风险等级">{selectedTask.risk_level ? formatRiskLevel(selectedTask.risk_level) : '-'}</Descriptions.Item>
              <Descriptions.Item label="状态"><Tag color={selectedTask.success ? 'success' : 'error'}>{selectedTask.success ? '成功' : '失败'}</Tag></Descriptions.Item>
              <Descriptions.Item label="总耗时">{formatMs(selectedTask.total_latency_ms)}</Descriptions.Item>
              <Descriptions.Item label="时间">{fromNow(selectedTask.timestamp)}</Descriptions.Item>
            </Descriptions>

            {/* Task Timeline */}
            <Card title="任务运行路径" size="small">
              <Timeline
                items={[
                  { color: 'blue', children: `边缘判断 (${selectedTask.confidence ? `${(selectedTask.confidence * 100).toFixed(1)}%` : '未知'})` },
                  ...(selectedTask.route === 'cloud' || selectedTask.route === 'edge_cloud'
                    ? [{ color: 'purple', children: '云端复核' }]
                    : []),
                  {
                    color: selectedTask.success ? 'green' : 'red',
                    children: `最终结论: ${selectedTask.label ? formatLabel(selectedTask.label) : '未知'}`,
                  },
                ]}
              />
            </Card>

            {/* Device Recommendation */}
            <Card title="设备运行建议" size="small">
              <Alert
                type={selectedTask.risk_level === 'high' || selectedTask.risk_level === 'critical' ? 'warning' : 'info'}
                showIcon
                icon={<InfoCircleOutlined />}
                description={
                  <Space direction="vertical" size={4}>
                    <Text strong>{getDeviceRecommendation(selectedTask)}</Text>
                    <Text style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
                      建议来源: 前端规则映射
                    </Text>
                  </Space>
                }
              />
            </Card>
          </Space>
        )}
      </Drawer>
    </div>
  );
}