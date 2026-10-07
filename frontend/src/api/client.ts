import axios from 'axios';

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
      return Promise.reject(new Error(message));
    }
    return Promise.reject(error);
  },
);
