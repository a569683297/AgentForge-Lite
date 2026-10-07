import type { ThemeConfig } from 'antd';
import { palette, type ThemeMode } from './palette';

const fontSans = "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif";
const fontMono = "'SF Mono', 'JetBrains Mono', Menlo, Consolas, 'Noto Sans Mono CJK SC', monospace";

function buildTheme(mode: ThemeMode): ThemeConfig {
  const p = palette[mode];
  return {
    token: {
      colorPrimary: p.accent,
      colorPrimaryHover: p.accentHover,
      colorLink: p.accent,
      colorLinkHover: p.accentHover,
      colorSuccess: p.success,
      colorWarning: p.warning,
      colorError: p.danger,
      colorInfo: p.info,
      colorBgLayout: p.canvas,
      colorBgContainer: p.surface,
      colorBgElevated: p.surfaceRaised,
      colorBorder: p.borderDefault,
      colorBorderSecondary: p.borderSubtle,
      colorText: p.textPrimary,
      colorTextSecondary: p.textSecondary,
      colorTextTertiary: p.textTertiary,
      colorTextQuaternary: p.textDisabled,
      colorTextDisabled: p.textDisabled,
      colorTextPlaceholder: p.textTertiary,
      fontSize: 13,
      fontSizeSM: 11,
      fontSizeLG: 15,
      controlHeight: 32,
      controlHeightSM: 28,
      controlHeightLG: 36,
      borderRadius: 6,
      borderRadiusSM: 4,
      borderRadiusLG: 6,
      borderRadiusXS: 2,
      lineHeight: 1.54,
      fontWeightStrong: 500,
      boxShadow: p.shadowOverlay,
      boxShadowSecondary: p.shadowOverlay,
      boxShadowTertiary: p.shadowOverlay,
      fontFamily: fontSans,
      fontFamilyCode: fontMono,
      lineWidthFocus: 2,
      controlOutlineWidth: 2,
    },
    components: {
      Layout: {
        headerBg: p.surface,
        siderBg: p.surface,
        bodyBg: p.canvas,
        headerHeight: 48,
        headerPadding: '0 16px',
      },
      Table: {
        headerBg: p.canvas,
        headerColor: p.textSecondary,
        headerSplitColor: 'transparent',
        borderColor: p.borderSubtle,
        rowHoverBg: p.canvas,
        cellFontSize: 13,
        cellFontSizeMD: 13,
        cellFontSizeSM: 12,
        cellPaddingBlock: 6,
        cellPaddingInline: 12,
        cellPaddingBlockMD: 4,
        cellPaddingInlineMD: 8,
        cellPaddingBlockSM: 2,
        cellPaddingInlineSM: 8,
      },
      Card: {
        bodyPadding: 16,
        bodyPaddingSM: 12,
        borderRadius: 6,
      },
      Tag: {
        borderRadiusSM: 3,
      },
    },
  };
}

export const lightTheme = buildTheme('light');
export const darkTheme = buildTheme('dark');
