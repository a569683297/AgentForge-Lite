import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { ThemeProvider } from './theme/ThemeProvider';
import './theme/tokens.css';
import './theme/global.css';

// BrowserRouter：用浏览器原生 History API 做路由（地址栏是干净的 /knowledge，
// 而不是 /#/knowledge）。代价是「刷新子路径」需要服务器把未知路径兜底回
// index.html —— frontend/nginx.conf 的 try_files 已经配好，无需加配置。
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ThemeProvider>
  </StrictMode>,
);
