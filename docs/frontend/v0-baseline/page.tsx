'use client'

import { useState } from 'react'
import {
  Activity,
  Archive,
  ArrowUpRight,
  BarChart3,
  Bot,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  Database,
  Download,
  FileCode2,
  FileText,
  FlaskConical,
  FolderOpen,
  Gauge,
  LayoutDashboard,
  Menu,
  MessageSquare,
  Moon,
  MoreHorizontal,
  Paperclip,
  PanelLeft,
  Play,
  Plus,
  Search,
  Send,
  Settings2,
  ShieldCheck,
  Sun,
  TerminalSquare,
  Upload,
  Wrench,
  X,
} from 'lucide-react'
import './dashboard.css'

type PageKey = 'chat' | 'knowledge' | 'eval' | 'analytics' | 'tools'

const sessions = [
  ['这个项目为什么不用 LlamaIndex？', '10:13'],
  ['最近三次评测的准确率趋势', '昨天'],
  ['MCP 的工具怎么注册进来的', '昨天'],
  ['切片大小改成多少合适', '10-02'],
  ['重排模型用的什么', '10-02'],
  ['Harness 的 11 个工具都是什么', '10-01'],
]

const documents = [
  ['AgentForge 技术选型说明.md', 'MD', 'ready', '48', '10-03 14:22'],
  ['MCP 协议要点.pdf', 'PDF', 'ready', '96', '10-03 14:25'],
  ['评测方法论.md', 'MD', 'ready', '72', '10-02 09:10'],
  ['消融实验记录-2026-09-30.md', 'MD', 'ready', '34', '10-02 09:12'],
  ['Harness 接入说明.docx', 'DOCX', 'ready', '61', '10-01 16:40'],
  ['RAG 检索链路设计.md', 'MD', 'ready', '55', '10-01 16:45'],
  ['老版架构图.pdf', 'PDF', 'failed', '—', '10-04 10:05'],
  ['面试问答整理.md', 'MD', 'processing', '—', '10-04 10:14'],
]

const nav = [
  { key: 'chat', label: '对话台', icon: MessageSquare },
  { key: 'knowledge', label: '知识库', icon: Database },
  { key: 'eval', label: '评测看板', icon: FlaskConical },
  { key: 'analytics', label: '分析', icon: BarChart3 },
  { key: 'tools', label: '工具注册表', icon: Wrench },
] as const

export default function Page() {
  const [active, setActive] = useState<PageKey>('chat')
  const [collapsed, setCollapsed] = useState(false)
  const [dark, setDark] = useState(false)
  const [citation, setCitation] = useState(false)
  const [query, setQuery] = useState('')

  const currentTitle = nav.find((item) => item.key === active)?.label ?? '对话台'

  return (
    <div className={dark ? 'app-shell dark' : 'app-shell'}>
      <aside className={collapsed ? 'sidebar collapsed' : 'sidebar'}>
        <div className="brand-row">
          {!collapsed && <><div className="brand-mark"><Bot size={15} /></div><span>AgentForge</span></>}
          <button className="icon-button" aria-label="折叠侧栏" onClick={() => setCollapsed(!collapsed)}>{collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}</button>
        </div>
        <div className="nav-section-label">工作台</div>
        <nav className="main-nav">
          {nav.map(({ key, label, icon: Icon }) => (
            <button key={key} className={active === key ? 'nav-item active' : 'nav-item'} onClick={() => setActive(key)} title={collapsed ? label : undefined}>
              <Icon size={16} /><span>{!collapsed && label}</span>{!collapsed && key === 'chat' && <span className="nav-count">3</span>}
            </button>
          ))}
        </nav>
        {!collapsed && <div className="history">
          <div className="history-header"><span>最近会话</span><MoreHorizontal size={14} /></div>
          {sessions.map(([title, time], index) => <button key={title} className={index === 0 ? 'session active' : 'session'} onClick={() => setActive('chat')}><span>{title}</span><small>{time}</small></button>)}
        </div>}
        <div className="sidebar-bottom">
          <button className="nav-item"><Settings2 size={16} /><span>{!collapsed && '设置'}</span></button>
          <div className="user-row"><div className="avatar">LW</div>{!collapsed && <><div><strong>Lin Wei</strong><small>工程团队</small></div><ChevronDown size={14} /></>}</div>
        </div>
      </aside>
      <main className="main-area">
        <header className="topbar">
          <div className="topbar-left"><PanelLeft size={15} className="mobile-menu" /><span>{currentTitle}</span><span className="breadcrumb-dot">/</span><span className="muted">production</span></div>
          <div className="topbar-right"><div className="api-status"><i /> API 正常</div><select className="model-select" defaultValue="deepseek-chat"><option>deepseek-chat</option><option>gpt-4o-mini</option></select><button className="icon-button" aria-label="切换主题" onClick={() => setDark(!dark)}>{dark ? <Sun size={16} /> : <Moon size={16} />}</button><button className="icon-button" aria-label="帮助"><CircleHelp size={16} /></button></div>
        </header>
        <div className="page-content">{active === 'chat' && <ChatPage query={query} setQuery={setQuery} onCitation={() => setCitation(true)} />}{active === 'knowledge' && <KnowledgePage />}{active === 'eval' && <EvalPage />}{active === 'analytics' && <AnalyticsPage />}{active === 'tools' && <ToolsPage />}</div>
      </main>
      {citation && <div className="drawer-backdrop" onClick={() => setCitation(false)}><aside className="citation-drawer" onClick={(e) => e.stopPropagation()}><div className="drawer-head"><div><span className="eyebrow">引用 [1]</span><h2>检索来源</h2></div><button className="icon-button" onClick={() => setCitation(false)}><X size={16} /></button></div><div className="source-title"><FileText size={17} /><div><strong>技术选型说明.md</strong><span>知识库 · 已就绪</span></div></div><div className="similarity"><span>余弦相似度</span><strong>0.83</strong></div><div className="source-copy">本项目检索层不引入 LlamaIndex。检索链路拆成 BM25 与向量两路，再使用 RRF 融合，最后经过一层重排。这样可以在消融实验时只替换单一环节。</div><button className="secondary-button full">在知识库中打开 <ArrowUpRight size={14} /></button></aside></div>}
    </div>
  )
}

function PageHeader({ title, description, action }: { title: string; description: string; action?: React.ReactNode }) { return <div className="page-header"><div><h1>{title}</h1><p>{description}</p></div>{action}</div> }

function ChatPage({ query, setQuery, onCitation }: { query: string; setQuery: (s: string) => void; onCitation: () => void }) {
  return <div className="chat-page"><div className="chat-toolbar"><div><strong>研究助手 · RAG 架构讨论</strong><span className="mono muted">a3f2c1d8-...</span></div><div className="toolbar-actions"><button className="secondary-button"><Plus size={14} /> 新建会话</button><button className="secondary-button"><Download size={14} /> 导出</button></div></div><div className="message-list"><div className="message user-message"><div className="message-label">你</div><p>这个项目为什么不用 LlamaIndex？</p><span className="message-meta">10:12:03</span></div><div className="message agent-message"><div className="message-label">Agent <span className="mono muted">deepseek-chat</span></div><div className="tool-call"><div className="tool-summary"><span className="status-dot success" /><span className="mono">search_documents</span><span className="tag">local</span><span className="mono muted tool-time">86ms</span><ChevronDown size={14} /></div><div className="tool-detail"><span className="detail-label">入参</span><code>{'{ query: "为什么不用 LlamaIndex", top_k: 5 }'}</code><span className="detail-label">返回摘要</span><code>命中 5 个片段，最高相似度 0.83</code></div></div><div className="tool-call"><div className="tool-summary"><span className="status-dot success" /><span className="mono">harness__list_pipelines</span><span className="tag">Harness</span><span className="mono muted tool-time">820ms</span><ChevronDown size={14} /></div></div><p>主要是因为控制权。<button className="citation-link" onClick={onCitation}>[1]</button> 项目把检索链路拆成了 BM25 与向量两路，再用 RRF 融合，最后过一层重排。LlamaIndex 把这些步骤封装在它自己的抽象里，做消融实验时不容易只改其中一环。<button className="citation-link" onClick={onCitation}>[2]</button></p><span className="message-meta mono">1240ms · 1,238 tokens · ¥0.003</span></div><div className="message user-message"><div className="message-label">你</div><p>那评测集现在多少条？</p><span className="message-meta">10:13:40</span></div><div className="message agent-message"><div className="message-label">Agent <span className="mono muted">deepseek-chat</span></div><p>当前 60 条，分四类：文档问答 30 条、跨文档推理 10 条、工具调用 10 条、多步分析 10 条。</p><span className="message-meta mono">980ms · 640 tokens · ¥0.0016</span></div></div><div className="composer"><div className="composer-tools"><button className="icon-button"><Paperclip size={15} /></button><span>Enter 发送 · Shift+Enter 换行</span></div><textarea value={query} onChange={(e) => setQuery(e.target.value)} placeholder="问点什么…" rows={2} /><div className="composer-bottom"><span className="muted">上下文：知识库 · 8 份文档</span><button className="primary-button"><Send size={14} /> 发送</button></div></div><div className="chat-status">本次会话 3 轮 · 2 次工具调用 · 累计 4,102 tokens <span>·</span> <span className="live-dot" /> 系统运行正常</div></div>
}

function KnowledgePage() { return <div><PageHeader title="知识库" description="管理可检索文档与索引处理状态。" action={<button className="primary-button"><Upload size={14} /> 上传文档</button>} /><div className="stat-strip"><strong>8</strong><span>份文档</span><i /><strong className="green-text">7</strong><span>已就绪</span><i /><strong className="amber-text">1</strong><span>处理中</span><i /><strong>412</strong><span>个切片</span></div><div className="table-card"><div className="table-toolbar"><div className="search-box"><Search size={14} /><input placeholder="搜索文件名" /></div><button className="secondary-button"><Archive size={14} /> 全部状态</button></div><table><thead><tr><th>文件名</th><th>类型</th><th>状态</th><th className="numeric">切片数</th><th>上传时间</th><th></th></tr></thead><tbody>{documents.map(([name, type, status, chunks, date]) => <tr key={name}><td><div className="file-cell"><div className={'file-icon ' + type.toLowerCase()}><FileText size={14} /></div><div><span>{name}</span>{status === 'processing' && <div className="progress-line"><i /></div>}{status === 'failed' && <small className="error-text">PDF 解析失败：文件已损坏或加密</small>}</div></div></td><td className="muted">{type}</td><td><span className="state"><i className={'status-dot ' + status} />{status === 'ready' ? '已就绪' : status === 'processing' ? '处理中 · 3.4s' : '失败'}</span></td><td className="numeric mono">{chunks}</td><td className="mono muted">{date}</td><td><button className="row-action">{status === 'failed' ? '重试' : '删除'}</button></td></tr>)}</tbody></table></div><div className="footnote">共 8 份文档 · 已就绪 7 · 处理中 1 · 共 412 个切片</div></div> }

function EvalPage() { const categories = [['doc_qa','文档问答','30','30','21','4.40','4.12','4.25'],['cross_doc','跨文档推理','10','10','5','3.80','3.40','3.70'],['tool_call','工具调用','10','10','7','—','—','—']]; return <div><PageHeader title="评测看板" description="比较检索配置，定位回答质量与失败模式。" action={<div className="toolbar-actions"><select className="model-select"><option>hybrid_rerank</option><option>hybrid</option><option>pure_vector</option></select><button className="primary-button"><Play size={14} /> 运行新评测</button></div>} /><div className="run-row">{[['hybrid_rerank','62.5%','09-30 18:22','32d4d63c','a7797b06'],['hybrid','58.3%','09-30 17:05','32d4d63c','a7797b06'],['pure_vector','50.0%','09-30 15:40','32d4d63c','a7797b06']].map((run,index) => <div className={index === 0 ? 'run-card selected' : 'run-card'} key={run[0]}><div className="run-card-top"><span className="mono">{run[0]}</span><strong>{run[1]}</strong></div><span className="muted mono">{run[2]}</span><div className="fingerprints"><span>题集 <b>{run[3]}…</b></span><span>语料 <b>{run[4]}…</b></span></div><small className="muted">deepseek-chat · 3 次生成</small></div>)}</div><div className="metric-grid">{[['准确率','62.5%','按题计算，每题多次生成先取多数结论'],['正确性均分','4.21','按行计算，分母是 48 行（有分数的行）'],['引用忠实度','3.88','分母同上'],['完整性','4.05','分母同上']].map(([label,value,desc]) => <div className="metric-card" key={label}><span>{label}</span><strong>{value}</strong><small>{desc}</small></div>)}</div><div className="split-grid"><div className="panel"><div className="panel-title"><h2>分类聚合</h2><span className="muted mono">当前运行 #6</span></div><table className="compact-table"><thead><tr><th>类别</th><th className="numeric">题数</th><th className="numeric">明细行数</th><th className="numeric">通过行数</th><th className="numeric">正确性</th><th className="numeric">忠实度</th><th className="numeric">完整性</th></tr></thead><tbody>{categories.map((row) => <tr key={row[0]}><td><strong>{row[1]}</strong><small className="muted mono">{row[0]}</small></td>{row.slice(2).map((cell,index) => <td key={index} className={(cell !== '—' && Number(cell) < 4) ? 'numeric amber-text' : 'numeric mono'}>{cell}</td>)}</tr>)}</tbody></table><p className="table-note">工具调用类题目只判定调用序列是否正确，不送 judge 判内容，因此三维度得分为空（显示为 —）。</p></div><div className="panel"><div className="panel-title"><h2>失败模式</h2><span className="muted">30 条未通过行</span></div><div className="failure-list">{[['检索未命中',12],['答案不完整',8],['引用越界',5],['工具选错',3],['其他',2]].map(([name,count]) => <div className="failure-row" key={name}><span>{name}</span><div className="failure-bar"><i style={{width: `${(Number(count)/12)*100}%`}} /></div><b>{count}</b></div>)}</div></div></div><div className="panel cases-panel"><div className="panel-title"><h2>逐题明细</h2><div className="toolbar-actions"><select className="small-select"><option>全部类别</option></select><select className="small-select"><option>全部结果</option></select></div></div><table className="compact-table"><thead><tr><th>题号</th><th>类别</th><th>答案摘要</th><th className="numeric">引用数</th><th>三维分数</th><th>结果</th><th>失败原因</th></tr></thead><tbody>{[['A03','文档问答','BM25 打分是应用层自己算的……','5','5 / 4 / 5',true,'—'],['A22','文档问答','重排模型使用 bge……','3','3 / 2 / 3',false,'低分'],['B05','跨文档推理','两份文档的口径不一致……','4','3 / 2 / 3',false,'答案不完整'],['C07','工具调用','我直接回答，不需要查询……','0','—',false,'工具选错']].map((row) => <tr key={row[0]}><td className="mono"><strong>{row[0]}</strong></td><td>{row[1]}</td><td className="truncate-cell">{row[2]}</td><td className="numeric mono">{row[3]}</td><td className="mono">{row[4]}</td><td><span className="state"><i className={'status-dot ' + (row[5] ? 'success' : 'failed')} />{row[5] ? '通过' : '未通过'}</span></td><td className="muted">{row[6]}</td></tr>)}</tbody></table></div></div> }

function AnalyticsPage() { return <div><PageHeader title="分析" description="观察 Agent 的调用成本、延迟与运行稳定性。" action={<button className="secondary-button"><Download size={14} /> 导出报告</button>} /><div className="metric-grid analytics-metrics">{[['总请求','12,842','过去 7 天 · +18.4%'],['平均延迟','1,240ms','P95 · 2,840ms'],['Token 消耗','1.24M','过去 7 天 · ¥32.40'],['成功率','98.7%','较上周期 +0.8%']].map(([a,b,c]) => <div className="metric-card" key={a}><span>{a}</span><strong>{b}</strong><small>{c}</small></div>)}</div><div className="analytics-grid"><div className="panel chart-panel"><div className="panel-title"><div><h2>请求与成功率</h2><span className="muted">过去 7 天</span></div><div className="legend"><i className="legend-line" />请求量 <i className="legend-bar" />成功请求</div></div><div className="fake-chart"><div className="chart-y"><span>3k</span><span>2k</span><span>1k</span><span>0</span></div><div className="chart-area"><div className="grid-lines" /><svg viewBox="0 0 700 200" preserveAspectRatio="none" aria-label="请求量趋势图"><path d="M0 155 C55 140 70 145 110 125 S180 130 220 105 S280 118 330 82 S390 98 440 62 S505 80 550 45 S620 62 700 25" fill="none" stroke="var(--accent)" strokeWidth="2" /><path d="M0 158 C55 143 70 148 110 128 S180 133 220 108 S280 121 330 85 S390 101 440 65 S505 83 550 48 S620 65 700 28 V200 H0Z" fill="var(--accent-soft)" /></svg><div className="chart-x"><span>09-28</span><span>09-29</span><span>09-30</span><span>10-01</span><span>10-02</span><span>10-03</span><span>10-04</span></div></div></div></div><div className="panel"><div className="panel-title"><h2>热门工具</h2><span className="muted">调用次数</span></div><div className="rank-list">{[['search_documents','4,218','82%'],['harness__list_pipelines','1,842','45%'],['current_time','928','22%'],['inventory__query_inventory','604','15%']].map(([name,count,width],i) => <div className="rank-item" key={name}><span className="rank-num">0{i+1}</span><div><strong className="mono">{name}</strong><div className="rank-bar"><i style={{width}} /></div></div><b className="mono">{count}</b></div>)}</div></div></div></div> }

function ToolsPage() { return <div><PageHeader title="工具注册表" description="已注册的 MCP 与本地工具，共 11 个。" action={<button className="primary-button"><Plus size={14} /> 注册工具</button>} /><div className="tool-filters"><div className="search-box"><Search size={14} /><input placeholder="搜索工具名称或描述" /></div><button className="filter-pill active">全部 <span>11</span></button><button className="filter-pill">MCP <span>7</span></button><button className="filter-pill">本地 <span>4</span></button></div><div className="tool-grid">{[['search_documents','知识库','搜索知识库中的相关片段','本地','86ms','ready'],['harness__list_pipelines','Harness','列出当前项目中的流水线及状态','MCP','820ms','ready'],['current_time','系统','获取当前系统时间','本地','12ms','ready'],['inventory__query_inventory','Inventory','查询库存与商品信息','MCP','340ms','warning'],['harness__get_run_logs','Harness','获取一次运行的完整日志','MCP','1.2s','ready'],['write_artifact','Artifacts','写入并版本化项目产物','本地','—','ready']].map(([name,group,desc,type,latency,status]) => <div className="tool-card" key={name}><div className="tool-card-head"><div className="tool-icon"><TerminalSquare size={16} /></div><span className="state"><i className={'status-dot ' + (status === 'warning' ? 'warning' : 'success')} />{status === 'warning' ? '需授权' : '可用'}</span></div><strong className="mono">{name}</strong><p>{desc}</p><div className="tool-card-foot"><span className="tag">{type}</span><span className="mono muted">平均 {latency}</span><button className="icon-button" aria-label="工具设置"><Settings2 size={14} /></button></div></div>)}</div></div> }
