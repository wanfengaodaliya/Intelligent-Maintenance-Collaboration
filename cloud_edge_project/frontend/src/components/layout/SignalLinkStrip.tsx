import { useMemo } from 'react';
import type { ServiceStatusMap } from '../../types';

interface SignalLinkStripProps {
  serviceStatus: ServiceStatusMap;
  isEdgeOnline: boolean;
  isSchedulerOnline: boolean;
  isCloudOnline: boolean;
}

interface LinkSegment {
  from: string;
  to: string;
  status: 'normal' | 'active' | 'stale' | 'broken' | 'unknown';
}

const NODE_LABELS: Record<string, string> = {
  device: '设备',
  edge: '边缘节点',
  scheduler: '调度器',
  cloud: '云端',
};

export default function SignalLinkStrip({
  serviceStatus,
  isEdgeOnline,
  isSchedulerOnline,
  isCloudOnline,
}: SignalLinkStripProps) {
  const segments: LinkSegment[] = useMemo(() => {
    const edgeSt = isEdgeOnline ? 'normal' : serviceStatus.edge === 'offline' ? 'broken' : 'stale';
    const schedSt = isSchedulerOnline ? 'normal' : serviceStatus.scheduler === 'offline' ? 'broken' : 'stale';
    const cloudSt = isCloudOnline ? 'normal' : serviceStatus.cloud === 'offline' ? 'broken' : 'stale';
    const networkSt = serviceStatus.network === 'offline' ? 'stale' : 'normal';

    return [
      { from: 'device', to: 'edge', status: networkSt },
      { from: 'edge', to: 'scheduler', status: edgeSt === 'normal' ? 'normal' : edgeSt },
      { from: 'scheduler', to: 'cloud', status: schedSt === 'normal' && cloudSt === 'normal' ? 'normal' : 'stale' },
    ];
  }, [serviceStatus, isEdgeOnline, isSchedulerOnline, isCloudOnline]);

  const getSegmentColor = (status: LinkSegment['status']) => {
    switch (status) {
      case 'normal': return 'var(--color-link-normal)';
      case 'active': return 'var(--color-link-active)';
      case 'stale': return 'var(--color-link-unknown)';
      case 'broken': return 'var(--color-link-broken)';
      default: return 'var(--color-link-unknown)';
    }
  };

  const getNodeBg = (nodeId: string) => {
    const seg = segments.find(s => s.from === nodeId);
    if (!seg) return 'var(--color-link-unknown)';
    return getSegmentColor(seg.status);
  };

  const getLastNodeBg = () => {
    const last = segments[segments.length - 1];
    return getSegmentColor(last?.status || 'unknown');
  };

  return (
    <div
      style={{
        height: 56,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 0,
        padding: '0 24px',
        background: '#fff',
        borderBottom: '1px solid var(--color-border-light)',
      }}
    >
      {segments.map((seg, idx) => (
        <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: 0 }}>
          <div
            style={{
              padding: '4px 12px',
              borderRadius: 4,
              fontSize: 12,
              fontWeight: 600,
              color: '#fff',
              background: idx === 0 ? getNodeBg('device') : getNodeBg(seg.from),
              whiteSpace: 'nowrap',
            }}
          >
            {NODE_LABELS[seg.from]}
          </div>
          <div
            style={{
              width: 60,
              height: 3,
              background: getSegmentColor(seg.status),
              position: 'relative',
              transition: 'background var(--transition-base)',
            }}
          >
            {(seg.status === 'normal' || seg.status === 'active') && (
              <div
                className="link-pulse"
                style={{
                  position: 'absolute',
                  left: 0,
                  top: -2,
                  width: 7,
                  height: 7,
                  borderRadius: '50%',
                  background: getSegmentColor(seg.status),
                  animation: 'linkPulse 2s ease-in-out infinite',
                }}
              />
            )}
            {seg.status === 'broken' && (
              <div
                style={{
                  position: 'absolute',
                  left: '50%',
                  top: -4,
                  fontSize: 14,
                  color: 'var(--color-link-broken)',
                  transform: 'translateX(-50%)',
                  lineHeight: 1,
                }}
              >
                &#x2716;
              </div>
            )}
          </div>
          {idx === segments.length - 1 && (
            <div
              style={{
                padding: '4px 12px',
                borderRadius: 4,
                fontSize: 12,
                fontWeight: 600,
                color: '#fff',
                background: getLastNodeBg(),
                whiteSpace: 'nowrap',
              }}
            >
              {NODE_LABELS.cloud}
            </div>
          )}
        </div>
      ))}
      <style>{`
        @keyframes linkPulse {
          0%, 100% { opacity: 0.4; }
          50% { opacity: 1; }
        }
        @media (prefers-reduced-motion: reduce) {
          .link-pulse {
            animation: none !important;
            opacity: 1 !important;
          }
        }
      `}</style>
    </div>
  );
}