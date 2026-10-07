# AgentForge Frontend Implementation Plan

> **For agentic workers:** This plan is executed inline in the current session with verification checkpoints.

**Goal:** Build the seven-page AgentForge-Lite engineering console in `frontend/` with Vite, React, TypeScript, Ant Design, real API integration where available, and explicit mock data where the backend is not ready.

**Architecture:** A single AntD `ConfigProvider` is driven by a shared theme context and `data-theme` attribute. Pages are selected from a typed `PageKey` state in `App.tsx`; API modules expose typed functions backed by one axios client, while `src/mock/index.ts` owns all unsupported data sources. CSS Modules handle the custom dense dashboard surfaces and CSS variables handle self-drawn areas and ECharts colors.

**Tech Stack:** Vite, React, TypeScript, Ant Design, `@ant-design/icons`, Axios, ECharts, Vitest.

---

### Task 1: Frontend scaffold and test harness

**Files:** create `frontend/package.json`, `frontend/index.html`, `frontend/vite.config.ts`, `frontend/tsconfig.json`, `frontend/tsconfig.node.json`, `frontend/src/test/setup.ts`, `frontend/vitest.config.ts`.

- [ ] Add dependencies and scripts for dev, build, preview, typecheck, and Vitest.
- [ ] Add the Vite `/api` proxy to `http://localhost:8000`.
- [ ] Add the first failing utility tests before production utility code.

### Task 2: Typed domain utilities and API layer

**Files:** create `frontend/src/types/api.ts`, `frontend/src/lib/format.ts`, `frontend/src/lib/chat.ts`, `frontend/src/api/client.ts`, `frontend/src/api/*.ts`.

- [ ] Implement citation parsing, relative date formatting, evaluation latest-by-config selection, and tool-message pairing with tests.
- [ ] Implement typed health, chat, sessions, documents, evaluation, and tools clients.
- [ ] Keep unsupported trace/retrieval/metrics data in `src/mock/index.ts` only.

### Task 3: Theme, shell, and reusable components

**Files:** create `frontend/src/theme/*`, `frontend/src/components/*`, `frontend/src/styles/*`, `frontend/src/App.tsx`, `frontend/src/main.tsx`.

- [ ] Add the palette, AntD theme builder, synchronized theme provider, and global CSS.
- [ ] Build the dense AppShell with sidebar, topbar, page header, status dot, skeleton, empty, metric, chart, citation, tool-call, and message components.
- [ ] Add typed seven-page navigation and responsive content layout.

### Task 4: Real API pages

**Files:** create `frontend/src/pages/ChatPage.tsx`, `KnowledgePage.tsx`, `EvalPage.tsx`, `ToolsPage.tsx`.

- [ ] Implement non-streaming chat with session persistence, citation drawer, tool-call expansion, and loading/error states.
- [ ] Implement document upload, polling, filtering, delete confirmation, and status summaries.
- [ ] Implement evaluation run selection, filters, scoring denominator notes, and case drill-down.
- [ ] Implement tools/server listing, source filtering, schema expansion, and disabled registration action.

### Task 5: Mock-first pages

**Files:** create `frontend/src/mock/index.ts`, `frontend/src/pages/TracePage.tsx`, `RetrieverPage.tsx`, `AnalyticsPage.tsx`.

- [ ] Add self-contained mock traces, retrieval ranks, and analytics points with source comments.
- [ ] Render the trace waterfall with 12+ spans and hover details.
- [ ] Render before/after retrieval ranks with logits explicitly labeled.
- [ ] Render a theme-aware ECharts analytics chart and mark the data as example data.

### Task 6: Verification

**Files:** none beyond fixes from verification.

- [ ] Run `npm test -- --run`.
- [ ] Run `npm run build`.
- [ ] Run `npm run typecheck`.
- [ ] Search source for forbidden gradient, emoji, and non-palette hex values.
- [ ] Inspect the built app and report any backend-unavailable states honestly.
