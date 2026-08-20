import type { TaskTrace, PacketTrace, UnifiedTask } from '../types';

export function isPacketTrace(trace: TaskTrace | PacketTrace): trace is PacketTrace {
  return 'packet_id' in trace;
}

export function unifyTask(trace: TaskTrace | PacketTrace): UnifiedTask {
  if (isPacketTrace(trace)) {
    return {
      id: trace.packet_id,
      source: trace.device_id,
      route: trace.route,
      label: trace.final_label,
      confidence: trace.final_confidence,
      risk_level: trace.risk_level,
      total_latency_ms: trace.total_latency_ms,
      success: trace.success,
      timestamp: trace.log_timestamp,
      recommendation_source: 'none',
    };
  }
  return {
    id: trace.task_id,
    source: trace.source_node,
    route: trace.route,
    label: trace.label,
    confidence: trace.confidence,
    total_latency_ms: trace.total_latency_ms,
    success: trace.success,
    timestamp: trace.timestamp,
    scenario: trace.scenario,
    recommendation_source: 'none',
  };
}

export function getDeviceRecommendation(task: UnifiedTask): string {
  if (task.recommendation) {
    return task.recommendation;
  }
  if (task.recommendation_source === 'backend' && task.recommendation) {
    return task.recommendation;
  }
  // Fallback: derive from status + risk level
  if (task.success && task.label === 'normal') {
    return '继续运行，保持监测。';
  }
  if (task.success && task.label === 'abnormal' && task.risk_level === 'low') {
    return '继续运行，保持监测。';
  }
  if (task.risk_level === 'medium') {
    return '降低负载并安排计划检查。';
  }
  if (task.risk_level === 'high') {
    return '尽快安排停机检修。';
  }
  if (task.risk_level === 'critical') {
    return '立即停机并进行人工复核。';
  }
  if (!task.label || !task.risk_level) {
    return '数据不足，暂无运行建议。';
  }
  return '继续运行，保持监测。';
}