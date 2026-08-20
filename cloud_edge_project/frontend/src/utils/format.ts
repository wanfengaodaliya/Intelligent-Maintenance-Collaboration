export function formatPercent(value: number, decimals: number = 2): string {
  return `${(value * 100).toFixed(decimals)}%`;
}

export function formatMs(value: number): string {
  if (value < 1) return `${(value * 1000).toFixed(1)} μs`;
  if (value < 1000) return `${value.toFixed(1)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

export function formatBytes(value: number): string {
  if (value < 1024) return `${value.toFixed(0)} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatKbps(value: number): string {
  if (value < 1000) return `${value.toFixed(0)} Kbps`;
  return `${(value / 1000).toFixed(2)} Mbps`;
}

export function formatScore(value: number): string {
  return `${value.toFixed(1)}%`;
}

export function formatRoute(route: string): string {
  const map: Record<string, string> = {
    edge: '边缘处理',
    cloud: '云端复核',
    edge_cloud: '边云协同',
    fallback_edge: '边缘降级',
  };
  return map[route] || route;
}

export function formatLabel(label: string | undefined): string {
  if (!label) return '未知';
  const map: Record<string, string> = {
    normal: '正常',
    abnormal: '异常',
  };
  return map[label] || label;
}

export function formatRiskLevel(level: string | undefined): string {
  if (!level) return '未知';
  const map: Record<string, string> = {
    low: '低风险',
    medium: '中风险',
    high: '高风险',
    critical: '严重风险',
  };
  return map[level] || level;
}

export function formatNodeStatus(status: string): string {
  const map: Record<string, string> = {
    online: '在线',
    stale: '陈旧',
    offline: '离线',
    unknown: '未知',
    ok: '正常',
    degraded: '降级',
    starting: '启动中',
    unavailable: '不可用',
  };
  return map[status] || status;
}