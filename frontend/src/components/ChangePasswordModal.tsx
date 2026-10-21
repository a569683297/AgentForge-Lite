import { Form, Input, Modal, Typography, message } from 'antd';
import { useState } from 'react';
import { changePassword } from '../api/auth';
import { ApiError } from '../api/client';

/**
 * 修改密码弹窗
 * =============
 * 为什么单独一个组件：它会从"用户菜单"里打开，而那个菜单属于**外壳**（AppShell）；
 * 把表单塞进 AppShell 会让那个本来就负责布局的文件再多一堆状态与请求。
 *
 * ⚠ 一个必须如实告诉用户的事实（弹窗里的那行小字）：
 *   改密后**已签发的 access token 仍然有效**（最长 2 小时）——它是无状态 JWT，
 *   服务端不查表就吊销不了。refresh token 会立刻作废。
 *   把这句话吞掉、只写"修改成功"，等于给用户一个错误的安全感。
 */
export function ChangePasswordModal({
  open,
  onClose,
  onChanged,
}: {
  open: boolean;
  onClose: () => void;
  /** 改密成功后回调（调用方负责清本地登录态并跳登录页） */
  onChanged: () => void;
}) {
  const [form] = Form.useForm<{ old_password: string; new_password: string; confirm: string }>();
  const [submitting, setSubmitting] = useState(false);

  async function handleOk() {
    const values = await form.validateFields();
    setSubmitting(true);
    try {
      await changePassword(values.old_password, values.new_password);
      message.success('密码已修改，请重新登录');
      form.resetFields();
      onChanged();
    } catch (reason: unknown) {
      // 旧密码错是 401，新密码不合规是 400 —— 两者文案都由后端给，直接展示
      message.error(reason instanceof ApiError ? reason.message : '修改失败，请稍后重试');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      open={open}
      title="修改密码"
      okText="确认修改"
      cancelText="取消"
      confirmLoading={submitting}
      onOk={() => void handleOk()}
      onCancel={() => { form.resetFields(); onClose(); }}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item
          name="old_password"
          label="原密码"
          rules={[{ required: true, message: '请输入原密码' }]}
        >
          <Input.Password autoComplete="current-password" />
        </Form.Item>
        <Form.Item
          name="new_password"
          label="新密码"
          rules={[
            { required: true, message: '请输入新密码' },
            { min: 8, message: '至少 8 位' },
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          name="confirm"
          label="确认新密码"
          dependencies={['new_password']}
          rules={[
            { required: true, message: '请再输入一次' },
            ({ getFieldValue }) => ({
              validator(_, value) {
                if (!value || getFieldValue('new_password') === value) return Promise.resolve();
                return Promise.reject(new Error('两次输入不一致'));
              },
            }),
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
          改密后，其它设备上的登录会在刷新令牌时失效；当前设备的访问凭证最长仍可用 2 小时。
        </Typography.Text>
      </Form>
    </Modal>
  );
}
