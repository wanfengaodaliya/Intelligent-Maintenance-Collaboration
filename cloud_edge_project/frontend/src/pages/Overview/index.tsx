import { useState, useEffect, useRef, useMemo } from 'react';
import { Row, Col, Card, Statistic, Table, Tag, Typography, Descriptions } from 'antd';
import ReactEChartsCore from 'echarts-for-react';
import { useMetrics } from '../../hooks/useMetrics';
import { formatPercent, formatMs } from '../../utils/format';
import LoadingSkeleton from '../../components/feedback/LoadingSkeleton';
import ErrorDisplay from '../../components/feedback/ErrorDisplay';
import type { DashboardMetrics } from '../../types';

const { Title, Text } = Typography;

interface SessionSnapshot {
  time: string;
  total_packets: number;
  success_rate: number;
  avg_latency_ms: number;
}

// Target matrix for competition metrics
const TARGET_MATRIX = [
  { key: 'success_rate', label: '任务成功率', target: '≥ 95%', unit: 'percent' },
  { key: 'avg_latency_ms', label: '平均延迟', target: '≤ 200ms', unit: 'ms' },
  { key: 'p95_latency_ms', label: 'P95 延迟', target: '≤ 500ms', unit: 'ms' },
  { key: 'weak_network_availability', label: '弱网可用率', target: '≥ 90%', unit: 'percent' },
  { key: 'conflict_resolve_rate', label: '冲突解决率', target: '≥ 85%', unit: 'percent' },
  { key: 'cloud_call_ratio', label: '云端调用率', target: '≤ 30%', unit: 'percent' },
];

export default function OverviewPage() {
  const { data: metrics, isLoading, error, refetch } = useMetrics();
  const [snapshots, setSnapshots] = useState<SessionSnapshot[]>([]);
  const prevMetricsRef = useRef<DashboardMetrics | null>(null);

  // Accumulate snapshots during session
  useEffect(() => {
    if (metrics && metrics !== prevMetricsRef.current) {
      prevMetricsRef.current = metrics;
      setSnapshots(prev => {
        const snapshot: SessionSnapshot = {
          time: new Date().toLocaleTimeString('zh-CN'),
          total_packets: metrics.total_packets,
          success_rate: metrics.success_rate,
          avg_latency_ms: metrics.avg_latency_ms,
        };
        const next = [...prev, snapshot];
        return next.slice(-30); // Keep last 30 snapshots
      });
    }
  }, [metrics]);

  const trendOptions = useMemo(() => {
    return {
      tooltip: { trigger: 'axis' as const },
      grid: { left: 50, right: 20, top: 30, bottom: 30 },
      xAxis: {
        type: 'category' as const,
        data: snapshots.map(s => s.time),
        axisLabel: { fontSize: 10, interval: Math.max(0, Math.floor(snapshots.length / 6)) },
      },
      yAxis: [
        { type: 'value' as const, name: '数量', position: 'left' as const },
        { type: 'value' as const, name: '延迟 (ms)', position: 'right' as const },
      ],
      series: [
        {
          name: '数据包数',
          type: 'line',
          data: snapshots.map(s => s.total_packets),
          smooth: true,
          lineStyle: { color: '#2B5FB8', width: 2 },
          itemStyle: { color: '#2B5FB8' },
          yAxisIndex: 0,
        },
        {
          name: '成功率',
          type: 'line',
          data: snapshots.map(s => +(s.success_rate * 100).toFixed(1)),
          smooth: true,
          lineStyle: { color: '#2C9AA0', width: 2 },
          itemStyle: { color: '#2C9AA0' },
          yAxisIndex: 0,
        },
        {
          name: '平均延迟(ms)',
          type: 'line',
          data: snapshots.map(s => +s.avg_latency_ms.toFixed(1)),
          smooth: true,
          lineStyle: { color: '#E59B2F', width: 2 },
          itemStyle: { color: '#E59B2F' },
          yAxisIndex: 1,
        },
      ],
      legend: { data: ['数据包数', '成功率', '平均延迟(ms)'], bottom: 0 },
    };
  }, [snapshots]);

  const routePieOptions = useMemo(() => {
    if (!metrics) return {};
    return {
      tooltip: { trigger: 'item' as const, formatter: '{b}: {c}% ({d}%)' },
      series: [{
        type: 'pie',
        radius: ['40%', '70%'],
        avoidLabelOverlap: true,
        itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
        label: { show: true, formatter: '{b}\n{d}%' },
        data: [
          { value: +(metrics.edge_only_ratio * 100).toFixed(1), name: '边缘处理', itemStyle: { color: '#2C9AA0' } },
          { value: +(metrics.cloud_call_ratio * 100).toFixed(1), name: '云端复核', itemStyle: { color: '#2B5FB8' } },
          { value: +(metrics.fallback_edge_ratio * 100).toFixed(1), name: '边缘降级', itemStyle: { color: '#E59B2F' } },
          { value: +((1 - metrics.edge_only_ratio - metrics.cloud_call_ratio - metrics.fallback_edge_ratio) * 100).toFixed(1), name: '边云协同', itemStyle: { color: '#5B8FF9' } },
        ],
      }],
    };
  }, [metrics]);

  if (isLoading) return <LoadingSkeleton type="card" count={6} />;
  
  if (error) {
    return (
      <div style={{ padding: 24 }}>
        <ErrorDisplay error={error} onRetry={() => refetch()} title="系统指标加载失败" />
      </div>
    );
  }

  if (!metrics) {
    return (
      <div style={{ padding: 24 }}>
        <ErrorDisplay error={{ error_code: 'NO_DATA', message: '暂无系统指标数据' }} onRetry={() => refetch()} />
      </div>
    );
  }

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <Title level={4} style={{ marginBottom: 16 }}>系统指标</Title>

      {/* Key Metrics Cards */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={8} lg={4}>
          <Card size="small"><Statistic title="数据包总数" value={metrics.total_packets} /></Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card size="small"><Statistic title="成功率" value={formatPercent(metrics.success_rate)} valueStyle={{ color: metrics.success_rate >= 0.95 ? 'var(--color-accent-green)' : 'var(--color-accent-amber)' }} /></Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card size="small"><Statistic title="平均延迟" value={formatMs(metrics.avg_latency_ms)} /></Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card size="small"><Statistic title="P95 延迟" value={formatMs(metrics.p95_latency_ms)} /></Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card size="small"><Statistic title="云端调用率" value={formatPercent(metrics.cloud_call_ratio)} /></Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card size="small"><Statistic title="异常占比" value={formatPercent(metrics.abnormal_ratio)} valueStyle={{ color: metrics.abnormal_ratio > 0.1 ? 'var(--color-accent-red)' : undefined }} /></Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        {/* Session Trend Chart */}
        <Col xs={24} lg={14}>
          <Card title="本次会话趋势" size="small" extra={
            <Text style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>基于浏览器会话中的轮询快照，不代表历史数据</Text>
          }>
            {snapshots.length > 1 ? (
              <ReactEChartsCore option={trendOptions} style={{ height: 280 }} />
            ) : (
              <div style={{ height: 280, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--color-text-secondary)' }}>
                等待数据积累...
              </div>
            )}
          </Card>
        </Col>

        {/* Route Distribution */}
        <Col xs={24} lg={10}>
          <Card title="任务路由分布" size="small">
            <ReactEChartsCore option={routePieOptions} style={{ height: 280 }} />
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        {/* Target Achievement Matrix */}
        <Col xs={24} lg={12}>
          <Card title="赛题指标达标矩阵" size="small">
            <Table
              dataSource={TARGET_MATRIX.map(item => {
                const value = metrics[item.key as keyof DashboardMetrics] as number;
                const achieved = item.key === 'success_rate' ? value >= 0.95
                  : item.key === 'avg_latency_ms' ? value <= 200
                  : item.key === 'p95_latency_ms' ? value <= 500
                  : item.key === 'weak_network_availability' ? value >= 0.9
                  : item.key === 'conflict_resolve_rate' ? value >= 0.85
                  : item.key === 'cloud_call_ratio' ? value <= 0.3
                  : false;
                return {
                  key: item.key,
                  label: item.label,
                  target: item.target,
                  current: item.unit === 'percent' ? formatPercent(value) : formatMs(value),
                  achieved,
                };
              })}
              columns={[
                { title: '指标', dataIndex: 'label', key: 'label' },
                { title: '目标', dataIndex: 'target', key: 'target' },
                { title: '当前值', dataIndex: 'current', key: 'current' },
                { title: '达标', dataIndex: 'achieved', key: 'achieved', render: (v: boolean) => <Tag color={v ? 'success' : 'error'}>{v ? '达标' : '未达标'}</Tag> },
              ]}
              pagination={false}
              size="small"
            />
          </Card>
        </Col>

        {/* Conflict & Abnormal Section */}
        <Col xs={24} lg={12}>
          <Card title="冲突与异常分析" size="small">
            <Descriptions column={2} size="small" bordered>
              <Descriptions.Item label="冲突率">{formatPercent(metrics.conflict_rate)}</Descriptions.Item>
              <Descriptions.Item label="冲突解决率">{formatPercent(metrics.conflict_resolve_rate)}</Descriptions.Item>
              <Descriptions.Item label="弱网可用率">{formatPercent(metrics.weak_network_availability)}</Descriptions.Item>
              <Descriptions.Item label="边缘降级率">{formatPercent(metrics.fallback_edge_ratio)}</Descriptions.Item>
              <Descriptions.Item label="异常占比">{formatPercent(metrics.abnormal_ratio)}</Descriptions.Item>
              <Descriptions.Item label="数据包总数">{metrics.total_packets}</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>

      {/* Metrics Explanation */}
      <Card title="指标口径说明" size="small" style={{ marginTop: 16 }}>
        <Descriptions column={1} size="small">
          <Descriptions.Item label="成功率">成功任务数 / 总任务数</Descriptions.Item>
          <Descriptions.Item label="平均延迟">所有任务总延迟的算术平均值</Descriptions.Item>
          <Descriptions.Item label="P95 延迟">按延迟排序，排在第 95 百分位的延迟值</Descriptions.Item>
          <Descriptions.Item label="云端调用率">经过云端处理的任务占比</Descriptions.Item>
          <Descriptions.Item label="冲突率">出现边缘与云端结论分歧的任务占比</Descriptions.Item>
          <Descriptions.Item label="冲突解决率">已解决的冲突占所有冲突的比例</Descriptions.Item>
          <Descriptions.Item label="弱网可用率">弱网场景下成功完成的任务比例</Descriptions.Item>
          <Descriptions.Item label="参考一致率">本系统不涉及人工真值，不称"模型准确率"</Descriptions.Item>
        </Descriptions>
      </Card>
    </div>
  );
}