import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';
import 'dayjs/locale/zh-cn';

dayjs.extend(relativeTime);
dayjs.locale('zh-cn');

export function fromNow(timestamp: string | number | Date): string {
  return dayjs(timestamp).fromNow();
}

export function formatTime(timestamp: string | number | Date, format: string = 'YYYY-MM-DD HH:mm:ss'): string {
  return dayjs(timestamp).format(format);
}

export function fromNs(ns: number): dayjs.Dayjs {
  return dayjs(Math.floor(ns / 1_000_000));
}

export function fromNsAgo(ns: number): string {
  return fromNs(ns).fromNow();
}

export function nsToMs(ns: number): number {
  return ns / 1_000_000;
}

export function nsToSeconds(ns: number): number {
  return ns / 1_000_000_000;
}

export function getTimeAgo(msTimestamp: number): string {
  return dayjs(msTimestamp).fromNow();
}

export function formatNsTimestamp(ns: number): string {
  if (ns <= 0) return '未知';
  return fromNs(ns).format('YYYY-MM-DD HH:mm:ss');
}

export function getNodeOnlineStatus(
  reportedAtNs: number,
  nowMs: number,
  staleThresholdMs: number,
  offlineThresholdMs: number
): 'online' | 'stale' | 'offline' | 'unknown' {
  if (!reportedAtNs || reportedAtNs <= 0) return 'unknown';
  const reportedMs = Math.floor(reportedAtNs / 1_000_000);
  const ageMs = nowMs - reportedMs;
  if (ageMs < staleThresholdMs) return 'online';
  if (ageMs < offlineThresholdMs) return 'stale';
  return 'offline';
}