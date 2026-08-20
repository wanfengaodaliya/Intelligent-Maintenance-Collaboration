export const SERVICE_NAMES = ['log', 'cloud', 'edge', 'scheduler', 'network'] as const;

export const SERVICE_LABELS: Record<string, string> = {
  log: '日志服务',
  cloud: '云端服务',
  edge: '边缘服务',
  scheduler: '调度器',
  network: '网络模拟',
};

export const SERVICE_PORTS: Record<string, number> = {
  log: 8006,
  cloud: 8004,
  edge: 8001,
  scheduler: 8003,
  network: 8090,
};

export const MODEL_UPDATE_STATES = [
  'created',
  'preparing_data',
  'training',
  'validating',
  'awaiting_confirmation',
  'approved',
  'distributing',
  'post_validating',
  'success',
  'rollback',
] as const;

export const MODEL_UPDATE_STATE_LABELS: Record<string, string> = {
  created: '创建',
  preparing_data: '数据准备',
  training: '训练中',
  validating: '验证中',
  awaiting_confirmation: '等待确认',
  approved: '已批准',
  distributing: '分发中',
  post_validating: '回验中',
  success: '成功',
  rollback: '已回滚',
};

export const ROUTE_COLORS: Record<string, string> = {
  edge: '#2C9AA0',
  cloud: '#2B5FB8',
  edge_cloud: '#5B8FF9',
  fallback_edge: '#E59B2F',
};