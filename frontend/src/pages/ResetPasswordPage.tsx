import { BulbOutlined, LockOutlined } from '@ant-design/icons';
import { Alert, Button, Form, Input, Result } from 'antd';
import { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { resetPassword } from '../api/auth';
import { ApiError } from '../api/client';
import { clearSession } from '../lib/auth';
import styles from '../styles/auth.module.css';

/**
 * 重置密码页（URL: /reset-password?token=...）
 *
 * 令牌从**查询参数**取，不是从路径里取：邮件链接的形态是
 * `/reset-password?token=xxx`（后端 auth_service.create_reset_token 拼的），
 * 换成路径参数就要同步改后端的拼接逻辑 —— 两处保持一致的成本高于收益。
 *
 * 三个必须知道的点：
 *  ① 令牌**一次性**：成功后立刻失效（后端 Redis 键已删）。再点一次旧链接会 401，
 *     所以成功后这里直接切到"已完成"状态，不留在表单上。
 *  ② 令牌**只存 Redis 30 分钟**（`AUTH_RESET_TTL_MIN`）。过期后页面不会提前知道，
 *     只有提交时才从 401 得知 —— 这是刻意不做的"预检"：
 *     预检接口会变成一个新的枚举面（拿 token 试探是否存在）。
 *  ③ 重置成功后**必须清本地登录态**：密码变了，旧的 refresh token 已经作废
 *     （后端比对 `password_changed_at`），留着只会让用户下一次请求时被踢。
 */

interface ResetForm {
  password: string;
  confirm: string;
}

export function ResetPasswordPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const token = params.get('token') ?? '';
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function handleSubmit(values: ResetForm) {
    setSubmitting(true);
    setError(null);
    try {
      await resetPassword(token, values.password);
      clearSession();
      setDone(true);
    } catch (reason: unknown) {
      setError(reason instanceof ApiError ? reason.message : '重置失败，请重新申请链接');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className={styles.wrap}>
      <div className={styles.card}>
        <div className={styles.brand}>
          <span className={styles.brandMark}><BulbOutlined /></span>
          <h1 className={styles.title}>设置新密码</h1>
        </div>

        {!token ? (
          <Result
            status="warning"
            title="链接不完整"
            subTitle="这个地址里没有重置令牌。请回到登录页重新申请一封重置邮件。"
            extra={<Button type="primary" onClick={() => navigate('/login', { replace: true })}>回登录页</Button>}
          />
        ) : done ? (
          <Result
            status="success"
            title="密码已重置"
            subTitle="请用新密码登录。此前签发的登录凭证已全部失效。"
            extra={<Button type="primary" onClick={() => navigate('/login', { replace: true })}>去登录</Button>}
          />
        ) : (
          <>
            <p className={styles.subtitle}>链接 30 分钟内有效，且只能使用一次。</p>
            {error ? (
              <Alert type="error" showIcon message={error} style={{ marginBottom: 14 }} />
            ) : null}
            <Form<ResetForm> layout="vertical" requiredMark={false} onFinish={handleSubmit}>
              <Form.Item
                name="password"
                label="新密码"
                rules={[
                  { required: true, message: '请输入新密码' },
                  { min: 8, message: '至少 8 位' },
                ]}
              >
                <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
              </Form.Item>
              <Form.Item
                name="confirm"
                label="确认新密码"
                dependencies={['password']}
                rules={[
                  { required: true, message: '请再输入一次' },
                  ({ getFieldValue }) => ({
                    validator(_, value) {
                      if (!value || getFieldValue('password') === value) return Promise.resolve();
                      return Promise.reject(new Error('两次输入不一致'));
                    },
                  }),
                ]}
              >
                <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
              </Form.Item>
              <Button type="primary" htmlType="submit" block loading={submitting}>
                重置密码
              </Button>
            </Form>
          </>
        )}
      </div>
    </div>
  );
}
