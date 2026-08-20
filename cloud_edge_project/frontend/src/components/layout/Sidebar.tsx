import { useNavigate, useLocation } from 'react-router-dom';
import { Layout, Menu } from 'antd';
import {
  DashboardOutlined,
  ProfileOutlined,
  ApartmentOutlined,
  HddOutlined,
  CloudServerOutlined,
} from '@ant-design/icons';

const { Sider } = Layout;

const menuItems = [
  {
    key: '/overview',
    icon: <DashboardOutlined />,
    label: '系统指标',
  },
  {
    key: '/tasks',
    icon: <ProfileOutlined />,
    label: '任务结果',
  },
  {
    key: '/network',
    icon: <ApartmentOutlined />,
    label: '网络链路',
  },
  {
    key: '/edge-nodes',
    icon: <HddOutlined />,
    label: '边缘节点',
  },
  {
    key: '/cloud',
    icon: <CloudServerOutlined />,
    label: '云端中心',
  },
];

export default function Sidebar() {
  const navigate = useNavigate();
  const location = useLocation();

  const selectedKey = '/' + location.pathname.split('/')[1];

  return (
    <Sider
      width={200}
      theme="dark"
      style={{
        overflow: 'auto',
        height: '100vh',
        position: 'fixed',
        left: 0,
        top: 0,
        bottom: 0,
        zIndex: 100,
      }}
    >
      <div
        style={{
          height: 48,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: '#fff',
          fontSize: 14,
          fontWeight: 600,
          letterSpacing: 1,
          borderBottom: '1px solid rgba(255,255,255,0.1)',
          padding: '0 16px',
          whiteSpace: 'nowrap',
        }}
      >
        云边协同运维台
      </div>
      <Menu
        theme="dark"
        mode="inline"
        selectedKeys={[selectedKey]}
        items={menuItems}
        onClick={({ key }) => navigate(key)}
        style={{ borderRight: 0, marginTop: 4 }}
      />
    </Sider>
  );
}