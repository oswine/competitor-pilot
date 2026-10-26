"use client";

import Link from "next/link";
import { useState } from "react";
import { apiHeaders } from "@/lib/api";

type Source = { source_id: string; enabled: boolean; config: { name: string; kind: string; origin: string; min_interval_seconds: number } };
type Target = { source_id: string; url: string; product_id: string; sku_id: string; role: "own" | "competitor"; label: string };
type Action = { category: string; priority: string; action: string; rationale: string; snapshot_ids: string[]; owner_role: string; deliverable: string; success_metric: string; prerequisites: string[] };
type Report = { report_id: string; status: string; created_at: string; snapshots: { snapshot_id: string; label: string; source_url: string; data: { title: string; currency: string | null; list_price: string | null; sale_price: string | null } }[]; changes: { change_id: string; current_snapshot_id: string; field: string; before: unknown; after: unknown; kind: string }[]; analysis: { summary: string; actions: Action[]; unknowns: string[]; review_insights: { theme: string; sentiment: string; observation: string; quotes: string[] }[] } };
type Job = { job_id: string; enabled: boolean; interval_seconds: number; attempts: number; last_error: string | null; next_run_at: string | null; request: { kind?: string; brief: string } };
const base = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8787";
const blankTarget = (): Target => ({ source_id: "", url: "", product_id: "", sku_id: "", role: "competitor", label: "" });

export default function CommercePage() {
  const [apiKey, setApiKey] = useState("");
  const [user, setUser] = useState("console-user");
  const [sources, setSources] = useState<Source[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [report, setReport] = useState<Report | null>(null);
  const [targets, setTargets] = useState<Target[]>([blankTarget()]);
  const [brief, setBrief] = useState("比较商品价格、促销和评价变化，给出商品信息优化、促销与素材制作建议。");
  const [interval, setInterval] = useState(3600);
  const [sourceName, setSourceName] = useState("");
  const [origin, setOrigin] = useState("");
  const [kind, setKind] = useState("authorized_json");
  const [permission, setPermission] = useState("");
  const [credential, setCredential] = useState("");
  const [minimum, setMinimum] = useState(60);
  const [renderJs, setRenderJs] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [pendingRun, setPendingRun] = useState<{ body: string; key: string } | null>(null);

  async function request<T>(path: string, method = "GET", body?: unknown, key?: string): Promise<T> {
    const response = await fetch(base + path, { method, headers: { ...apiHeaders(apiKey, user), ...(key ? { "Idempotency-Key": key } : {}) }, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }
  async function refresh() {
    const [s, j, r] = await Promise.all([request<Source[]>("/v1/commerce/sources"), request<Job[]>("/v1/monitor-jobs"), request<Report[]>("/v1/commerce/reports")]);
    setSources(s); setJobs(j.filter(x => x.request.kind === "commerce")); setReports(r);
  }
  async function perform(work: () => Promise<void>) {
    setBusy(true); setError(""); setMessage("");
    try { await work(); } catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败"); }
    finally { setBusy(false); }
  }
  function edit(index: number, key: keyof Target, value: string) {
    setTargets(targets.map((t, i) => i === index ? { ...t, [key]: value } : t));
  }
  async function run(scheduled: boolean) {
    const body = { kind: "commerce", brief, targets };
    if (scheduled) {
      const result = await request<{ job_id: string }>("/v1/commerce/monitor-jobs", "POST", { request: body, interval_seconds: interval });
      setMessage("已创建监控任务：" + result.job_id + "。后台 worker 启动后按周期执行。");
    } else {
      const serialized = JSON.stringify(body);
      const key = pendingRun?.body === serialized ? pendingRun.key : crypto.randomUUID();
      setPendingRun({ body: serialized, key });
      const result = await request<Report>("/v1/commerce/runs", "POST", body, key);
      setReport(result); setPendingRun(null);
      setMessage("分析已完成，运营建议待审核。");
    }
    await refresh();
  }
  async function download(format: string) {
    if (!report) return;
    const response = await fetch(base + "/v1/commerce/reports/" + report.report_id + "/export?format=" + format, { headers: apiHeaders(apiKey, user) });
    if (!response.ok) throw new Error(await response.text());
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a"); anchor.href = url;
    anchor.download = report.report_id + (format === "bundle" ? ".zip" : format === "markdown" ? ".md" : ".json");
    anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return <main className="main" style={{ maxWidth: 1400, margin: "0 auto" }}>
    <header className="topbar"><div><Link href="/">返回研究台</Link><h1>商品运营监控</h1><p>记录商品变化，将可核查的证据转为运营任务。</p></div></header>
    <section className="panel"><div className="panel-body"><div className="row">
      <label>访问密钥<input type="password" value={apiKey} onChange={e => setApiKey(e.target.value)} autoComplete="off" /></label>
      <label>操作人<input value={user} onChange={e => setUser(e.target.value)} /></label>
      <button className="primary" disabled={busy || !apiKey} onClick={() => perform(refresh)}>载入数据</button>
    </div>{error && <div className="error" role="alert">{error}</div>}{message && <p role="status">{message}</p>}</div></section>
    <div className="grid" style={{ marginTop: 20 }}>
      <section className="panel"><div className="panel-header"><h2>登记数据源</h2></div><div className="panel-body form-grid">
        <label>名称<input value={sourceName} onChange={e => setSourceName(e.target.value)} /></label>
        <label>来源方式<select value={kind} onChange={e => { setKind(e.target.value); setCredential(""); if (e.target.value === "taobao_open_api") setOrigin("https://gw.api.taobao.com"); }}><option value="authorized_json">授权商品数据接口</option><option value="taobao_open_api">淘宝官方接口（已授权店铺）</option><option value="public_product_page">允许采集的公开商品页</option></select></label>
        <label>网站根地址<input placeholder="https://shop.example.com" value={origin} onChange={e => setOrigin(e.target.value)} /></label>
        <label>授权记录或允许使用的规则说明<textarea value={permission} onChange={e => setPermission(e.target.value)} /></label>
        {kind === "authorized_json" && <label>服务器凭证名称（可选）<input placeholder="CI_SOURCE_SHOP_TOKEN" value={credential} onChange={e => setCredential(e.target.value)} /></label>}
        {kind === "taobao_open_api" && <><label>服务器淘宝凭证名称（必填）<input placeholder="CI_SOURCE_SHOP_TAOBAO_CREDENTIALS" value={credential} onChange={e => setCredential(e.target.value)} /></label><p className="hint">需要应用权限与店铺授权，不支持任意未授权竞品。服务器凭证 JSON 包含 tenant_id、app_key、app_secret、session；不要在此输入密钥。目标地址填写 https://gw.api.taobao.com/router/rest，并提供准确商品 ID / SKU。此接口不提供促销成交价或评价。</p></>}
        {kind === "public_product_page" && <label style={{ display: "flex", alignItems: "center", gap: 8 }}><input style={{ width: "auto" }} type="checkbox" checked={renderJs} onChange={e => setRenderJs(e.target.checked)} />该商品页需要动态渲染</label>}
        <label>最短访问间隔（秒）<input type="number" min={10} value={minimum} onChange={e => setMinimum(Number(e.target.value))} /></label>
        <button className="primary" disabled={busy || (kind === "taobao_open_api" && !credential)} onClick={() => perform(async () => { await request("/v1/commerce/sources", "POST", { name: sourceName, origin, kind, authorization_reference: permission, min_interval_seconds: minimum, render_js: kind === "public_product_page" && renderJs, credential_env: kind === "authorized_json" && credential ? credential : null, taobao_credentials_env: kind === "taobao_open_api" ? credential : null }); await refresh(); setMessage("数据源已登记。登记不代表接口权限已验证；首次运行会执行授权检查。"); })}>登记（管理员）</button>
        <p className="hint">数据接口需提供标准化商品字段；公开商品页需包含可匹配的商品结构化数据。凭证由服务器保管。</p>
        {sources.map(s => <div key={s.source_id}>{s.config.name} · {s.enabled ? "启用" : "暂停"} <button disabled={busy} onClick={() => perform(async () => { await request("/v1/commerce/sources/" + s.source_id, "PATCH", { enabled: !s.enabled }); await refresh(); })}>{s.enabled ? "暂停来源" : "启用来源"}</button></div>)}
      </div></section>
      <section className="panel"><div className="panel-header"><h2>研究目标</h2></div><div className="panel-body form-grid">
        <label>运营目标<textarea value={brief} onChange={e => setBrief(e.target.value)} /></label>
        {targets.map((t, i) => <fieldset key={i} className="form-grid"><legend>商品 {i + 1}</legend>
          <label>数据源<select value={t.source_id} onChange={e => edit(i, "source_id", e.target.value)}><option value="">请选择</option>{sources.filter(s => s.enabled).map(s => <option key={s.source_id} value={s.source_id}>{s.config.name}</option>)}</select></label>
          <div className="row"><label>商品名称<input value={t.label} onChange={e => edit(i, "label", e.target.value)} /></label><label>归属<select value={t.role} onChange={e => edit(i, "role", e.target.value)}><option value="competitor">竞品</option><option value="own">自有商品</option></select></label></div>
          <div className="row"><label>商品 ID<input value={t.product_id} onChange={e => edit(i, "product_id", e.target.value)} /></label><label>SKU<input value={t.sku_id} onChange={e => edit(i, "sku_id", e.target.value)} /></label></div>
          <label>商品页或授权接口地址<input value={t.url} onChange={e => edit(i, "url", e.target.value)} /></label>
          {targets.length > 1 && <button onClick={() => setTargets(targets.filter((_, n) => i !== n))}>移除此商品</button>}
        </fieldset>)}
        <button className="secondary" disabled={targets.length >= 20} onClick={() => setTargets([...targets, blankTarget()])}>添加商品</button>
        <label>监控周期（秒）<input type="number" min={60} max={2592000} value={interval} onChange={e => setInterval(Number(e.target.value))} /></label>
        <div className="actions"><button className="primary" disabled={busy} onClick={() => perform(() => run(false))}>{busy ? "处理中…" : pendingRun ? "重试／恢复分析" : "立即分析"}</button><button className="secondary" disabled={busy} onClick={() => perform(() => run(true))}>创建持续监控</button></div>
      </div></section>
      <section className="panel"><div className="panel-header"><h2>监控任务</h2></div><div className="panel-body">{jobs.length === 0 && <p>暂无商品监控任务。</p>}{jobs.map(job => <article className="finding" key={job.job_id}><p>{job.request.brief}</p><p>{job.enabled ? "启用" : "暂停"} · 连续失败 {job.attempts} 次 · 下次 {job.next_run_at ? new Date(job.next_run_at).toLocaleString() : "待调度"}</p>{job.last_error && <p className="error">{job.last_error}</p>}<button disabled={busy} onClick={() => perform(async () => { await request("/v1/monitor-jobs/" + job.job_id, "PATCH", { enabled: !job.enabled }); await refresh(); })}>{job.enabled ? "暂停后续执行" : "恢复"}</button></article>)}</div></section>
      <section className="panel"><div className="panel-header"><h2>历史报告</h2></div><div className="panel-body">{reports.map(r => <p key={r.report_id}><button onClick={() => setReport(r)}>{new Date(r.created_at).toLocaleString()} · {r.status} · {r.changes.length} 项变化</button></p>)}</div></section>
    </div>
    {report && <section className="panel" style={{ marginTop: 20 }}><div className="panel-header"><h2>运营报告</h2><span>{report.status}</span></div><div className="panel-body">
      <p>{report.analysis.summary}</p><h3>商品与来源</h3>{report.snapshots.map(s => <p key={s.snapshot_id}><a href={s.source_url} target="_blank" rel="noreferrer">{s.label}: {s.data.title}</a> · {s.data.currency ?? ""} {s.data.sale_price !== null ? s.data.sale_price : s.data.list_price !== null ? s.data.list_price + "（标价，非促销成交价）" : "价格未提供"}</p>)}
      <h3>观测变化</h3>{report.changes.length === 0 && <p>首次观测建立基线，或本次没有字段变化。</p>}{report.changes.map(c => <p key={c.change_id}>{report.snapshots.find(s => s.snapshot_id === c.current_snapshot_id)?.label} · {c.field}: {JSON.stringify(c.before)} → {JSON.stringify(c.after)} ({c.kind})</p>)}
      <h3>评价主题</h3>{report.analysis.review_insights.map((r, i) => <article key={i}><strong>{r.theme} · {r.sentiment}</strong><p>{r.observation}</p>{r.quotes.map((q, j) => <blockquote key={j}>{q}</blockquote>)}</article>)}
      <h3>运营任务建议</h3>{report.analysis.actions.map((a, i) => <article className="finding" key={i}><strong>{a.category} · {a.priority}</strong><p>{a.action}</p><p>{a.rationale}</p><p>负责角色：{a.owner_role} · 验证指标：{a.success_metric}</p><p>交付内容：{a.deliverable}</p><p>执行前提：{a.prerequisites.join("；")}</p><p>证据：{a.snapshot_ids.map(id => report.snapshots.find(s => s.snapshot_id === id)?.label ?? id).join("、")}</p></article>)}
      <h3>待确认事项</h3>{report.analysis.unknowns.map((u, i) => <p key={i}>{u}</p>)}
      <div className="actions">{["json", "markdown", "bundle"].map(f => <button key={f} disabled={busy} onClick={() => perform(() => download(f))}>导出 {f}</button>)}<button disabled={busy || report.status === "approved"} onClick={() => perform(async () => { await request("/v1/commerce/reports/" + report.report_id + "/approve", "POST"); setReport({ ...report, status: "approved" }); await refresh(); })}>审核通过</button></div>
    </div></section>}
  </main>;
}
