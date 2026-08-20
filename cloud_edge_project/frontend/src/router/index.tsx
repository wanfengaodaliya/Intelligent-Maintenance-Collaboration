import { createBrowserRouter, Navigate } from 'react-router-dom';
import AppLayout from '../components/layout/AppLayout';
import OverviewPage from '../pages/Overview';
import TasksPage from '../pages/Tasks';
import NetworkPage from '../pages/Network';
import EdgeNodesPage from '../pages/EdgeNodes';
import CloudPage from '../pages/Cloud';

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppLayout />,
    children: [
      { index: true, element: <Navigate to="/overview" replace /> },
      { path: 'overview', element: <OverviewPage /> },
      { path: 'tasks', element: <TasksPage /> },
      { path: 'network', element: <NetworkPage /> },
      { path: 'edge-nodes', element: <EdgeNodesPage /> },
      { path: 'cloud', element: <CloudPage /> },
    ],
  },
]);