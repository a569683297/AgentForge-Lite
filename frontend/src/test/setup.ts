import '@testing-library/jest-dom';

// React 18/19 的 `act()` 要求显式声明"当前在测试环境"，否则每次调用都会打印
// "The current testing environment is not configured to support act(...)"。
// 不设也能跑（只是噪音），但噪音会掩盖真正需要被看见的警告 —— 见
// src/lib/authStore.test.tsx。
declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
