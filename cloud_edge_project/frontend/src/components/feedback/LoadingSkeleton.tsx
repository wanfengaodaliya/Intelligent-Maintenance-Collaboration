import { Skeleton, Card, Row, Col } from 'antd';

interface LoadingSkeletonProps {
  type?: 'card' | 'table' | 'detail';
  count?: number;
}

export default function LoadingSkeleton({ type = 'card', count = 4 }: LoadingSkeletonProps) {
  if (type === 'table') {
    return (
      <Card>
        <Skeleton active paragraph={{ rows: 8 }} />
      </Card>
    );
  }

  if (type === 'detail') {
    return (
      <Card>
        <Skeleton active avatar paragraph={{ rows: 6 }} />
      </Card>
    );
  }

  return (
    <Row gutter={[16, 16]}>
      {Array.from({ length: count }).map((_, i) => (
        <Col xs={24} sm={12} lg={8} xl={6} key={i}>
          <Card>
            <Skeleton active paragraph={{ rows: 2 }} />
          </Card>
        </Col>
      ))}
    </Row>
  );
}