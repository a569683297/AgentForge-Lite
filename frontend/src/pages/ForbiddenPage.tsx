import { Button, Result } from 'antd';
import { useNavigate } from 'react-router-dom';

/**
 * 无权限页（403）
 * ================
 * 为什么**不直接静默重定向**回对话台：
 *   用户明确点了某个入口（或打开了旧书签），结果"什么都没发生"会被理解成页面坏了。
 *   一个明确的"你没有这个权限 + 需要哪一项 + 回得去的按钮"，才能让人知道
 *   是自己权限不够，而不是系统出 bug。
 *
 * 为什么把权限点名显示出来：
 *   这是内部平台，权限点不是机密；而"权限不足"这种笼统文案，用户既没法自查、
 *   也没法向管理员描述清楚缺什么。可排障优先于少显示一个字符串。
 *
 * ⚠ 本页只是**说明**，不是安全边界：真正拦住数据的是后端每个接口上的权限依赖。
 */
export function ForbiddenPage({ permission }: { permission?: string }) {
  const navigate = useNavigate();
  return (
    <Result
      status="403"
      title="没有访问权限"
      subTitle={
        permission
          ? `这个页面需要「${permission}」权限，当前账号没有。如果你认为应该能访问，请联系管理员调整角色。`
          : '当前账号没有访问这个页面的权限。'
      }
      extra={
        <Button type="primary" onClick={() => navigate('/chat')}>
          回到对话台
        </Button>
      }
    />
  );
}
