import { BulbOutlined, LockOutlined, MailOutlined, UserOutlined } from '@ant-design/icons';
import { Alert, Button, Form, Input, Tabs, Typography, message } from 'antd';
import { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { forgotPassword, login, register } from '../api/auth';
import { ApiError } from '../api/client';
import { saveSession } from '../lib/auth';
import styles from '../styles/auth.module.css';

/**
 * 登录 / 注册页
 * =================
 * 一块表单干两件事（Tabs 切换），因为两者的字段几乎一样，
 * 拆成两个页面只会让"我要注册"多一次跳转。
 *
 * 三个刻意的处理：
 *
 * 1. **登录后回到原页面**（`?next=`）
 *    拦截器踢人时会带上当前地址（见 api/client.ts 的 forceLogout）。
 *    不处理 next 的话，用户在 /eval 被踢出去、登录完落在 /chat，
 *    还得自己再点回去 —— 这是最容易被忽略的一处体验损失。
 *    ⚠ next 是 URL 参数，**必须只接受站内相对路径**（见 safeNext）：
 *    直接把 next 丢给 navigate 会造成开放重定向（把用户送到钓鱼站）。
 *
 * 2. **错误文案原样展示后端的话**
 *    后端对"用户不存在"和"密码错"返回同一句"邮箱或密码错误"（防枚举）。
 *    前端若自己再加工（比如"这个邮箱没注册过"），等于把这个防线拆了。
 *
 * 3. **"忘记密码"在页内展开，不跳新页面**
 *    本地开发时后端会回 `dev_link`（未配 SMTP），把它显示出来，
 *    整条找回链路就能在浏览器里走通 —— 否则这块功能等于没法验收。
 */

function safeNext(raw: string | null): string {
  // 只允许站内绝对路径：必须以单个 "/" 开头，且不能是 "//host"（协议相对 URL）
  if (!raw || !raw.startsWith('/') || raw.startsWith('//')) return '/chat';
  return raw;
}

interface LoginForm {
  email: string;
  password: string;
}

interface RegisterForm extends LoginForm {
  display_name?: string;
}

export function LoginPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const next = safeNext(params.get('next'));
  const [tab, setTab] = useState<'login' | 'register'>('login');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ---- 忘记密码 ----
  const [forgotOpen, setForgotOpen] = useState(false);
  const [forgotEmail, setForgotEmail] = useState('');
  const [forgotSending, setForgotSending] = useState(false);
  const [forgotResult, setForgotResult] = useState<{ message: string; dev_link: string | null } | null>(null);

  async function handleLogin(values: LoginForm) {
    setSubmitting(true);
    setError(null);
    try {
      const data = await login(values.email.trim(), values.password);
      saveSession(data);
      // replace：登录页不该留在历史里 —— 否则登录后按"后退"又回到登录页
      navigate(next, { replace: true });
    } catch (reason: unknown) {
      setError(reason instanceof ApiError ? reason.message : '登录失败，请稍后重试');
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRegister(values: RegisterForm) {
    setSubmitting(true);
    setError(null);
    try {
      const data = await register(values.email.trim(), values.password, values.display_name?.trim() ?? '');
      saveSession(data);
      message.success('注册成功，已自动登录');
      navigate(next, { replace: true });
    } catch (reason: unknown) {
      // 后端把"密码过长（>72 字节）"这类输入问题回 400 + 人话，直接展示
      setError(reason instanceof ApiError ? reason.message : '注册失败，请稍后重试');
    } finally {
      setSubmitting(false);
    }
  }

  async function handleForgot() {
    const email = forgotEmail.trim();
    if (!email) {
      message.warning('请先填写邮箱');
      return;
    }
    setForgotSending(true);
    try {
      const data = await forgotPassword(email);
      setForgotResult(data);
    } catch (reason: unknown) {
      // 这个接口**永远**回 200（防邮箱枚举），能失败的基本只有网络/503
      message.error(reason instanceof ApiError ? reason.message : '发送失败，请稍后重试');
    } finally {
      setForgotSending(false);
    }
  }

  return (
    <div className={styles.wrap}>
      <div className={styles.card}>
        <div className={styles.brand}>
          <span className={styles.brandMark}><BulbOutlined /></span>
          <h1 className={styles.title}>AgentForge</h1>
        </div>
        <p className={styles.subtitle}>企业级 Agent 运行时 · 登录后才能使用知识库与对话</p>

        {error ? (
          <Alert
            type="error"
            showIcon
            message={error}
            style={{ marginBottom: 14 }}
            closable
            onClose={() => setError(null)}
          />
        ) : null}

        <Tabs
          activeKey={tab}
          onChange={(key) => { setTab(key as 'login' | 'register'); setError(null); }}
          items={[
            {
              key: 'login',
              label: '登录',
              children: (
                <Form<LoginForm>
                  layout="vertical"
                  requiredMark={false}
                  onFinish={handleLogin}
                  autoComplete="on"
                >
                  <Form.Item
                    name="email"
                    label="邮箱"
                    rules={[{ required: true, message: '请输入邮箱' }]}
                  >
                    <Input prefix={<MailOutlined />} placeholder="you@example.com" autoComplete="username" />
                  </Form.Item>
                  <Form.Item
                    name="password"
                    label="密码"
                    rules={[{ required: true, message: '请输入密码' }]}
                  >
                    <Input.Password prefix={<LockOutlined />} autoComplete="current-password" />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" block loading={submitting}>
                    登录
                  </Button>
                  <div className={styles.hint}>
                    <Typography.Link onClick={() => { setForgotOpen((open) => !open); setForgotResult(null); }}>
                      {forgotOpen ? '收起找回密码' : '忘记密码？'}
                    </Typography.Link>
                  </div>

                  {forgotOpen ? (
                    <div style={{ marginTop: 10 }}>
                      <Input
                        prefix={<MailOutlined />}
                        placeholder="填写注册邮箱，发送重置链接"
                        value={forgotEmail}
                        onChange={(event) => setForgotEmail(event.target.value)}
                      />
                      <Button
                        style={{ marginTop: 8 }}
                        block
                        loading={forgotSending}
                        onClick={() => void handleForgot()}
                      >
                        发送重置链接
                      </Button>
                      {forgotResult ? (
                        <>
                          <Alert
                            style={{ marginTop: 8 }}
                            type="success"
                            showIcon
                            message={forgotResult.message}
                          />
                          {forgotResult.dev_link ? (
                            <div className={styles.devLink}>
                              开发环境直连链接（未配置邮件通道时返回）：<br />
                              <Typography.Link href={forgotResult.dev_link}>
                                {forgotResult.dev_link}
                              </Typography.Link>
                            </div>
                          ) : null}
                        </>
                      ) : null}
                    </div>
                  ) : null}
                </Form>
              ),
            },
            {
              key: 'register',
              label: '注册',
              children: (
                <Form<RegisterForm>
                  layout="vertical"
                  requiredMark={false}
                  onFinish={handleRegister}
                >
                  <Form.Item
                    name="email"
                    label="邮箱"
                    rules={[{ required: true, message: '请输入邮箱' }]}
                  >
                    <Input prefix={<MailOutlined />} placeholder="you@example.com" autoComplete="username" />
                  </Form.Item>
                  <Form.Item
                    name="display_name"
                    label="显示名（可留空）"
                  >
                    <Input prefix={<UserOutlined />} placeholder="例如：郭工" maxLength={64} />
                  </Form.Item>
                  <Form.Item
                    name="password"
                    label="密码"
                    rules={[
                      { required: true, message: '请输入密码' },
                      { min: 8, message: '至少 8 位' },
                      // 后端真正的上限是 **72 字节（UTF-8）**，不是 72 个字符；
                      // 中文一个字 3 字节，所以这里按字符数只能给一个宽松提示，
                      // 精确拒绝仍由后端做（返回 400 + 具体字节数）
                      { max: 72, message: '密码过长（中文建议不超过 24 个字）' },
                    ]}
                  >
                    <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" block loading={submitting}>
                    注册并登录
                  </Button>
                  <div className={styles.hint}>
                    密码只存 bcrypt 哈希，服务端也无法还原。空库时**第一个注册的账号自动成为管理员**。
                  </div>
                </Form>
              ),
            },
          ]}
        />

        <div className={styles.footer}>本地部署 · 数据只存在你自己的 PostgreSQL 里</div>
      </div>
    </div>
  );
}
