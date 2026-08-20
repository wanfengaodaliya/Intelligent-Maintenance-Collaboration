export function nsToMs(ns: number): number {
  return ns / 1_000_000;
}

export function nsToSeconds(ns: number): number {
  return ns / 1_000_000_000;
}

export function nsToFormattedTime(ns: number): string {
  const date = new Date(ns / 1_000_000);
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function getAgeFromNs(ns: number, nowMs: number): number {
  return nowMs - ns / 1_000_000;
}