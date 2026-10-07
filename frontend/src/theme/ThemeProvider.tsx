import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { ConfigProvider } from 'antd';
import { darkTheme, lightTheme } from './antdTheme';
import type { ThemeMode } from './palette';

interface ThemeContextValue {
  mode: ThemeMode;
  toggle: () => void;
}

const STORAGE_KEY = 'af-theme';
const ThemeContext = createContext<ThemeContextValue>({ mode: 'light', toggle: () => undefined });

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}

function initialTheme(): ThemeMode {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === 'dark' || saved === 'light') return saved;
  return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ThemeMode>(initialTheme);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', mode);
    document.documentElement.style.colorScheme = mode;
    localStorage.setItem(STORAGE_KEY, mode);
  }, [mode]);

  const value = useMemo(() => ({
    mode,
    toggle: () => setMode((current) => current === 'dark' ? 'light' : 'dark'),
  }), [mode]);

  return (
    <ThemeContext.Provider value={value}>
      <ConfigProvider theme={mode === 'dark' ? darkTheme : lightTheme}>
        {children}
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}
