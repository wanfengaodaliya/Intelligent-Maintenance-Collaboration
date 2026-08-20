import { useState } from 'react';
import { Card, Row, Col, Input, Button, Typography, Descriptions, Tag, Space, Timeline, Statistic, Alert, Modal, message, Tabs } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import { useAppConfig } from '../../hooks/useAppConfig';
import { useReviewQuery, useReviewSummary, useModelUpdate, useApproveModelUpdate, useRejectModelUpdate, useHandoffDistribution, useRequestRollback } from '../../hooks/useCloud';
import { useServiceHealth } from '../../hooks/useServiceHealth';
import { useTasks } from '../../hooks/useTasks';
import { formatPercent } from '../../utils/format';
import { fromNow } from '../../utils/time';
import { MODEL_UPDATE_STATE_LABELS } from '../../utils/constants';
import LoadingSkeleton from '../../components/feedback/LoadingSkeleton';
import ErrorDisplay from '../../components/feedback/ErrorDisplay';

const { Title, Text } = Typography;

const UPDATE_STATE_COLORS: Record<string, string> = {
  created: 'default',
  preparing_data: 'processing',
  training: 'processing',
  validating: 'processing',
  awaiting_confirmation: 'warning',
  approved: 'success',
  distributing: 'processing',
  post_validating: 'processing',
  success: 'success',
  rollback: 'error',
};

export default function CloudPage() {
  const config = useAppConfig();
  const { data: health, isLoading: healthLoading, error: healthError, refetch: refetchHealth } = useServiceHealth();
  const { data: tasks } = useTasks(50);

  // Review query
  const [reviewId, setReviewId] = useState('');
  const reviewType = 'packet' as const;
  const [submittedReviewId, setSubmittedReviewId] = useState<string | null>(null);
  const { data: review, isLoading: reviewLoading, error: reviewError, refetch: refetchReview } = useReviewQuery(reviewType, submittedReviewId);
  const { data: summary } = useReviewSummary(submittedReviewId);

  // Model update query
  const [updateId, setUpdateId] = useState('');
  const [submittedUpdateId, setSubmittedUpdateId] = useState<string | null>(null);
  const { data: modelUpdate, isLoading: updateLoading, error: updateError, refetch: refetchUpdate } = useModelUpdate(submittedUpdateId);

  // Mutations
  const approveMutation = useApproveModelUpdate();
  const rejectMutation = useRejectModelUpdate();
  const handoffMutation = useHandoffDistribution();
  const rollbackMutation = useRequestRollback();

  // Recent review IDs (stored in local state only)
  const [recentReviewIds, setRecentReviewIds] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem('recentReviewIds') || '[]');
    } catch { return []; }
  });
  const [recentUpdateIds, setRecentUpdateIds] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem('recentUpdateIds') || '[]');
    } catch { return []; }
  });

  const handleReviewSearch = () => {
    if (!reviewId.trim()) return;
    setSubmittedReviewId(reviewId.trim());
    const updated = [reviewId.trim(), ...recentReviewIds.filter(id => id !== reviewId.trim())].slice(0, 5);
    setRecentReviewIds(updated);
    localStorage.setItem('recentReviewIds', JSON.stringify(updated));
  };

  const handleUpdateSearch = () => {
    if (!updateId.trim()) return;
    setSubmittedUpdateId(updateId.trim());
    const updated = [updateId.trim(), ...recentUpdateIds.filter(id => id !== updateId.trim())].slice(0, 5);
    setRecentUpdateIds(updated);
    localStorage.setItem('recentUpdateIds', JSON.stringify(updated));
  };

  const handleApprove = () => {
    if (!submittedUpdateId) return;
    Modal.confirm({
      title: '确认批准模型更新',
      content: `确定批准模型更新 ${submittedUpdateId} 吗？当前状态: ${modelUpdate?.status || '未知'}`,
      onOk: async () => {
        try {
          await approveMutation.mutateAsync(submittedUpdateId);
          message.success('已批准');
          refetchUpdate();
        } catch (err: unknown) {
          message.error(`批准失败: ${(err as { message?: string })?.message || '未知错误'}`);
        }
      },
    });
  };

  const handleReject = () => {
    if (!submittedUpdateId) return;
    Modal.confirm({
      title: '确认拒绝模型更新',
      content: `确定拒绝模型更新 ${submittedUpdateId} 吗？`,
      onOk: async () => {
        try {
          await rejectMutation.mutateAsync(submittedUpdateId);
          message.success('已拒绝');
          refetchUpdate();
        } catch (err: unknown) {
          message.error(`拒绝失败: ${(err as { message?: string })?.message || '未知错误'}`);
        }
      },
    });
  };

  const handleHandoff = () => {
    if (!submittedUpdateId) return;
    Modal.confirm({
      title: '确认分发模型更新',
      content: `确定分发模型更新 ${submittedUpdateId} 到边缘节点吗？`,
      onOk: async () => {
        try {
          await handoffMutation.mutateAsync(submittedUpdateId);
          message.success('已发起分发');
          refetchUpdate();
        } catch (err: unknown) {
          message.error(`分发失败: ${(err as { message?: string })?.message || '未知错误'}`);
        }
      },
    });
  };

  const handleRollback = () => {
    if (!submittedUpdateId) return;
    Modal.confirm({
      title: '确认请求回滚',
      content: `确定请求回滚模型更新 ${submittedUpdateId} 吗？此操作将回滚到上一个版本。`,
      onOk: async () => {
        try {
          await rollbackMutation.mutateAsync(submittedUpdateId);
          message.success('已请求回滚');
          refetchUpdate();
        } catch (err: unknown) {
          message.error(`回滚失败: ${(err as { message?: string })?.message || '未知错误'}`);
        }
      },
    });
  };

  // Cloud review tasks from dashboard
  const cloudTasks = (tasks || []).filter(t => t.route === 'cloud' || t.route === 'edge_cloud');

  return (
    <div>
      <Title level={4} style={{ marginBottom: 16 }}>云端中心</Title>

      <Tabs
        defaultActiveKey="status"
        items={[
          {
            key: 'status',
            label: '云端服务状态',
            children: (
              <Row gutter={[16, 16]}>
                <Col xs={24} lg={12}>
                  <Card title="服务健康" size="small">
                    {healthLoading ? <LoadingSkeleton type="detail" /> :
                     healthError ? <ErrorDisplay error={healthError} onRetry={() => refetchHealth()} /> :
                     <Descriptions column={1} size="small" bordered>
                       <Descriptions.Item label="云端服务">
                         <Tag color={health?.cloud === 'online' ? 'success' : 'error'}>
                           {health?.cloud === 'online' ? '在线' : '离线'}
                         </Tag>
                       </Descriptions.Item>
                       <Descriptions.Item label="调度器">
                         <Tag color={health?.scheduler === 'online' ? 'success' : 'error'}>
                           {health?.scheduler === 'online' ? '在线' : '离线'}
                         </Tag>
                       </Descriptions.Item>
                       <Descriptions.Item label="日志服务">
                         <Tag color={health?.log === 'online' ? 'success' : 'error'}>
                           {health?.log === 'online' ? '在线' : '离线'}
                         </Tag>
                       </Descriptions.Item>
                       <Descriptions.Item label="网络模拟">
                         <Tag color={health?.network === 'online' ? 'success' : 'error'}>
                           {health?.network === 'online' ? '在线' : '离线'}
                         </Tag>
                       </Descriptions.Item>
                     </Descriptions>
                    }
                  </Card>
                </Col>
                <Col xs={24} lg={12}>
                  <Card title="近期云端复核任务" size="small">
                    {cloudTasks.length > 0 ? (
                      cloudTasks.slice(0, 10).map(task => (
                        <div key={task.id} style={{ padding: '4px 0', borderBottom: '1px solid var(--color-border-light)', display: 'flex', justifyContent: 'space-between' }}>
                          <code>{task.id}</code>
                          <Tag color={task.success ? 'success' : 'error'}>{task.success ? '成功' : '失败'}</Tag>
                          <Text style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>{fromNow(task.timestamp)}</Text>
                        </div>
                      ))
                    ) : (
                      <Text type="secondary">暂无云端复核任务数据</Text>
                    )}
                  </Card>
                </Col>
              </Row>
            ),
          },
          {
            key: 'review',
            label: '复核结果查询',
            children: (
              <Space direction="vertical" size={16} style={{ width: '100%' }}>
                <Card size="small">
                  <Space>
                    <Input
                      placeholder="输入 review_id 查询..."
                      prefix={<SearchOutlined />}
                      value={reviewId}
                      onChange={e => setReviewId(e.target.value)}
                      onPressEnter={handleReviewSearch}
                      style={{ width: 300 }}
                      allowClear
                    />
                    <Button type="primary" onClick={handleReviewSearch} loading={reviewLoading}>查询</Button>
                  </Space>
                  {recentReviewIds.length > 0 && (
                    <div style={{ marginTop: 8 }}>
                      <Text style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
                        最近访问: {recentReviewIds.map(id => (
                          <Button type="link" size="small" key={id} onClick={() => { setReviewId(id); setSubmittedReviewId(id); }}>
                            {id}
                          </Button>
                        ))}
                      </Text>
                      <Text style={{ fontSize: 10, color: 'var(--color-text-secondary)', display: 'block', marginTop: 4 }}>
                        仅保存本地最近访问记录，不代表服务器完整列表
                      </Text>
                    </div>
                  )}
                </Card>

                {reviewLoading && <LoadingSkeleton type="detail" />}
                {reviewError && <ErrorDisplay error={reviewError} onRetry={() => refetchReview()} />}

                {review && !reviewLoading && (
                  <Card title="复核结果" size="small">
                    <Descriptions column={2} size="small" bordered>
                      <Descriptions.Item label="复核ID"><code>{review.review_id}</code></Descriptions.Item>
                      <Descriptions.Item label="类型">{review.review_type}</Descriptions.Item>
                      <Descriptions.Item label="状态"><Tag color={review.status === 'completed' ? 'success' : 'processing'}>{review.status}</Tag></Descriptions.Item>
                      <Descriptions.Item label="争议">{review.dispute ? <Tag color="warning">有争议</Tag> : '无'}</Descriptions.Item>
                      {review.edge_conclusion && <Descriptions.Item label="边缘结论">{review.edge_conclusion}</Descriptions.Item>}
                      {review.cloud_conclusion && <Descriptions.Item label="云端结论">{review.cloud_conclusion}</Descriptions.Item>}
                      {review.edge_confidence !== undefined && <Descriptions.Item label="边缘置信度">{formatPercent(review.edge_confidence)}</Descriptions.Item>}
                      {review.cloud_confidence !== undefined && <Descriptions.Item label="云端置信度">{formatPercent(review.cloud_confidence)}</Descriptions.Item>}
                    </Descriptions>
                  </Card>
                )}

                {summary && !reviewLoading && (
                  <Card title="复核摘要" size="small">
                    <pre style={{ fontSize: 12, maxHeight: 300, overflow: 'auto', background: 'var(--color-bg-primary)', padding: 12, borderRadius: 6 }}>
                      {JSON.stringify(summary, null, 2)}
                    </pre>
                  </Card>
                )}
              </Space>
            ),
          },
          {
            key: 'model',
            label: '模型更新',
            children: (
              <Space direction="vertical" size={16} style={{ width: '100%' }}>
                <Card size="small">
                  <Space>
                    <Input
                      placeholder="输入 update_id 查询..."
                      prefix={<SearchOutlined />}
                      value={updateId}
                      onChange={e => setUpdateId(e.target.value)}
                      onPressEnter={handleUpdateSearch}
                      style={{ width: 300 }}
                      allowClear
                    />
                    <Button type="primary" onClick={handleUpdateSearch} loading={updateLoading}>查询</Button>
                  </Space>
                  {recentUpdateIds.length > 0 && (
                    <div style={{ marginTop: 8 }}>
                      <Text style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
                        最近访问: {recentUpdateIds.map(id => (
                          <Button type="link" size="small" key={id} onClick={() => { setUpdateId(id); setSubmittedUpdateId(id); }}>
                            {id}
                          </Button>
                        ))}
                      </Text>
                      <Text style={{ fontSize: 10, color: 'var(--color-text-secondary)', display: 'block', marginTop: 4 }}>
                        仅保存本地最近访问记录，不代表服务器完整列表
                      </Text>
                    </div>
                  )}
                </Card>

                {updateLoading && <LoadingSkeleton type="detail" />}
                {updateError && <ErrorDisplay error={updateError} onRetry={() => refetchUpdate()} />}

                {modelUpdate && !updateLoading && (
                  <>
                    <Card title="模型更新信息" size="small">
                      <Descriptions column={2} size="small" bordered>
                        <Descriptions.Item label="更新ID"><code>{modelUpdate.update_id}</code></Descriptions.Item>
                        <Descriptions.Item label="状态">
                          <Tag color={UPDATE_STATE_COLORS[modelUpdate.status] || 'default'}>
                            {MODEL_UPDATE_STATE_LABELS[modelUpdate.status] || modelUpdate.status}
                          </Tag>
                        </Descriptions.Item>
                        <Descriptions.Item label="模型版本"><code>{modelUpdate.model_version || '-'}</code></Descriptions.Item>
                        <Descriptions.Item label="数据集包数">{modelUpdate.dataset_packet_count ?? '-'}</Descriptions.Item>
                        {modelUpdate.created_at && <Descriptions.Item label="创建时间">{fromNow(modelUpdate.created_at)}</Descriptions.Item>}
                        {modelUpdate.confirmed_by && <Descriptions.Item label="确认人">{modelUpdate.confirmed_by}</Descriptions.Item>}
                      </Descriptions>
                    </Card>

                    {/* Model Update Timeline */}
                    <Card title="模型更新生命周期" size="small">
                      <Timeline
                        items={[
                          { color: 'blue', children: '创建' },
                          { color: modelUpdate.status === 'preparing_data' || ['training', 'validating', 'awaiting_confirmation', 'approved', 'distributing', 'post_validating', 'success'].includes(modelUpdate.status) ? 'blue' : 'gray', children: '数据准备' },
                          { color: modelUpdate.status === 'training' || ['validating', 'awaiting_confirmation', 'approved', 'distributing', 'post_validating', 'success'].includes(modelUpdate.status) ? 'blue' : 'gray', children: '训练' },
                          { color: modelUpdate.status === 'validating' || ['awaiting_confirmation', 'approved', 'distributing', 'post_validating', 'success'].includes(modelUpdate.status) ? 'blue' : 'gray', children: '验证' },
                          { color: modelUpdate.status === 'awaiting_confirmation' ? 'orange' : ['approved', 'distributing', 'post_validating', 'success'].includes(modelUpdate.status) ? 'blue' : 'gray', children: '等待确认' },
                          { color: modelUpdate.status === 'approved' || ['distributing', 'post_validating', 'success'].includes(modelUpdate.status) ? 'green' : 'gray', children: '批准' },
                          { color: modelUpdate.status === 'distributing' || ['post_validating', 'success'].includes(modelUpdate.status) ? 'blue' : 'gray', children: '分发' },
                          { color: modelUpdate.status === 'post_validating' || modelUpdate.status === 'success' ? 'blue' : 'gray', children: '回验' },
                          { color: modelUpdate.status === 'success' ? 'green' : modelUpdate.status === 'rollback' ? 'red' : 'gray', children: modelUpdate.status === 'rollback' ? '已回滚' : '成功' },
                        ]}
                      />
                    </Card>

                    {/* Training Results */}
                    {modelUpdate.training_result && (
                      <Card title="训练结果" size="small">
                        <Row gutter={16}>
                          {Object.entries(modelUpdate.training_result).map(([key, value]) => (
                            <Col span={6} key={key}>
                              <Statistic title={key} value={typeof value === 'number' ? formatPercent(value) : String(value)} />
                            </Col>
                          ))}
                        </Row>
                      </Card>
                    )}

                    {/* Write Actions (guarded by VITE_ENABLE_CLOUD_ACTIONS) */}
                    {config.enableCloudActions && (
                      <Card title="模型更新操作" size="small">
                        {!config.enableCloudActions ? (
                          <Alert type="info" showIcon message="云侧写操作已禁用" description="设置 VITE_ENABLE_CLOUD_ACTIONS=true 以启用" />
                        ) : (
                          <Space wrap>
                            <Button
                              type="primary"
                              onClick={handleApprove}
                              loading={approveMutation.isPending}
                              disabled={modelUpdate.status !== 'awaiting_confirmation'}
                            >
                              批准
                            </Button>
                            <Button
                              danger
                              onClick={handleReject}
                              loading={rejectMutation.isPending}
                              disabled={modelUpdate.status !== 'awaiting_confirmation'}
                            >
                              拒绝
                            </Button>
                            <Button
                              onClick={handleHandoff}
                              loading={handoffMutation.isPending}
                              disabled={modelUpdate.status !== 'approved'}
                            >
                              分发到边缘
                            </Button>
                            <Button
                              danger
                              onClick={handleRollback}
                              loading={rollbackMutation.isPending}
                              disabled={modelUpdate.status !== 'success'}
                            >
                              请求回滚
                            </Button>
                          </Space>
                        )}
                        <div style={{ marginTop: 8 }}>
                          <Text style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
                            所有操作均需二次确认，提交期间禁止重复点击
                          </Text>
                        </div>
                      </Card>
                    )}

                    {!config.enableCloudActions && (
                      <Alert
                        type="info"
                        showIcon
                        message="云侧写操作已禁用"
                        description="当前 VITE_ENABLE_CLOUD_ACTIONS 未设置为 true，模型更新操作（批准、拒绝、分发、回滚）不可用。如需启用，请在 .env 文件中设置 VITE_ENABLE_CLOUD_ACTIONS=true。"
                      />
                    )}
                  </>
                )}
              </Space>
            ),
          },
        ]}
      />
    </div>
  );
}