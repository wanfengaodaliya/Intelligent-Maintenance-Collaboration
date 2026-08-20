import { useState, useEffect, useCallback, useRef } from 'react';
import { Outlet } from 'react-router-dom';
import { Layout } from 'antd';
import dayjs from 'dayjs';
import Sidebar from './Sidebar';
import GlobalStatusBar from './GlobalStatusBar';
import SignalLinkStrip from './SignalLinkStrip';
import { checkAllServices } from '../../api/health';
import { useAppConfig } from '../../hooks/useAppConfig';
import type { ServiceStatusMap } from '../../types';

const { Content } = Layout;

export default function AppLayout() {
  const config = useAppConfig();
  const [currentTime, setCurrentTime] = useState(dayjs().format('HH:mm:ss'));
  const [serviceStatus, setServiceStatus] = useState<ServiceStatusMap>({
    log: 'unknown',
    cloud: 'unknown',
    edge: 'unknown',
    scheduler: 'unknown',
    network: 'unknown',
  });
  const [pollingEnabled, setPollingEnabled] = useState(true);
  const [pollingInterval, setPollingInterval] = useState(config.pollIntervalMs);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [dataSource, setDataSource] = useState('未知');
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const serviceUrls = {
    log: config.logApiBaseUrl,
    cloud: config.cloudApiBaseUrl,
    edge: config.edgeApiBaseUrl,
    scheduler: config.schedulerApiBaseUrl,
    network: config.networkApiBaseUrl,
  };

  const checkServices = useCallback(async () => {
    try {
      const status = await checkAllServices(serviceUrls);
      setServiceStatus(status);
      setLastUpdated(new Date());
      const onlineServices = Object.values(status).filter(s => s === 'online').length;
      setDataSource(`实时接口 (${onlineServices}/5 在线)`);
    } catch {
      // Partial failure is handled inside checkAllServices
    }
  }, [serviceUrls]);

  useEffect(() => {
    const timer = setInterval(() => {
      setCurrentTime(dayjs().format('HH:mm:ss'));
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    checkServices();
  }, [checkServices]);

  useEffect(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    if (pollingEnabled) {
      intervalRef.current = setInterval(checkServices, pollingInterval);
    }
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [pollingEnabled, pollingInterval, checkServices]);

  const isEdgeOnline = serviceStatus.edge === 'online';
  const isSchedulerOnline = serviceStatus.scheduler === 'online';
  const isCloudOnline = serviceStatus.cloud === 'online';

  return (
    <Layout style={{ height: '100vh', overflow: 'hidden' }}>
      <Sidebar />
      <Layout style={{ marginLeft: 200, height: '100vh', overflow: 'hidden' }}>
        <GlobalStatusBar
          currentTime={currentTime}
          serviceStatus={serviceStatus}
          pollingEnabled={pollingEnabled}
          pollingInterval={pollingInterval}
          onTogglePolling={setPollingEnabled}
          onChangeInterval={setPollingInterval}
          onManualRefresh={checkServices}
          lastUpdated={lastUpdated}
          isMockMode={config.enableMock}
          dataSource={dataSource}
        />
        <SignalLinkStrip
          serviceStatus={serviceStatus}
          isEdgeOnline={isEdgeOnline}
          isSchedulerOnline={isSchedulerOnline}
          isCloudOnline={isCloudOnline}
        />
        <Content
          style={{
            padding: '16px 20px',
            overflow: 'auto',
            height: 'calc(100vh - 48px - 56px)',
          }}
        >
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}