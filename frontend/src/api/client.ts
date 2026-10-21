import axios from 'axios';

/**
 * 带 HTTP 状态码的接口错误。
 *
 * 为什么需要状态码：同一个「上传失败」有三种完全不同的应对 ——
 * 400 让用户换文件、409 说明这份文档已经在了（刷新列表即可）、500 才是真的故障。
 * 只保留 message 字符串时，页面只能靠「文案里有没有某个词」判断，改一个字就坏。
 */
export class ApiError extends Error {
  readonly status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export const client = axios.create({
  baseURL: '/api',
  timeout: 20_000,
  headers: { 'Content-Type': 'application/json' },
});

client.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (axios.isAxiosError(error)) {
      const message = typeof error.response?.data?.detail === 'string'
        ? error.response.data.detail
        : error.message;
      return Promise.reject(new ApiError(message, error.response?.status));
    }
    return Promise.reject(error);
  },
);
