"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { actionReport, Claim, createResearch, Evidence, exportUrl, Finding, getRun, Mode, Report, TraceEvent } from "@/lib/api";

const initialBrief = "比较 Notion 与 Coda 的企业版价格、AI 功能和安全能力";

export default function Home() {
  const [brief, setBrief] = useState(initialBrief);
  const [competitors, setCompetitors] = useState("Notion, Coda");
  const [urls, setUrls] = useState("");
  const [mode, setMode] = useState<Mode>("auto");
  const [apiKey, setApiKey] = useState("");
  const [userId, setUserId] = useState("console-user");
  const [runId, setRunId] = useState("");
  const [report, setReport] = useState<Report | null>(null);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [claims, setClaims] = useState<Claim[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [comment, setComment] = useState("");
  const [approved, setApproved] = useState(false);

  const selectedMode = useMemo(() => {
    const text = brief.toLowerCase();
    if (mode !== "auto") return mode;
    if (["持续监控", "持续跟踪", "定期", "告警", "monitor", "track"].some((term) => text.includes(term))) return "monitor";
    if (["验证", "核实", "是否属实", "verify", "confirm"].some((term) => text.includes(term))) return "verify";
    return "investigation";
  }, [brief, mode]);

  useEffect(() => {
    if (!runId || report) return;
    const timer = window.setInterval(async () => {
      try {
        const current = await getRun(runId, apiKey, userId);
        setReport(current.report); setTrace(current.trace.events); setEvidence(current.evidence ?? []);
      } catch { /* The initial POST may still be committing. */ }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [runId, report, apiKey, userId]);

  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(""); setReport(null); setTrace([]); setEvidence([]); setClaims([]); setApproved(false);
    try {
      const response = await createResearch({ brief, competitors: competitors.split(",").map((item) => item.trim()).filter(Boolean), urls: urls.split(/\s+/).filter(Boolean), mode }, apiKey, userId, crypto.randomUUID());
      setRunId(response.trace.run_id); setReport(response.report); setTrace(response.trace.events); setEvidence(response.evidence ?? []);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Research request failed"); } finally { setBusy(false); }
  }

  async function approve() {
    if (!report) return; setError("");
    try { await actionReport(report.report_id, comment, apiKey, userId); setApproved(true); setReport({ ...report, status: "approved" }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Approval failed"); }
  }

  const findings: Finding[] = report?.findings ?? [];
  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">S</span><span>Signal Desk</span></div>
      <nav className="nav"><button className="active">Research desk</button><button>Monitor jobs</button><button>Evidence ledger</button><button>Replay archive</button></nav>
      <div className="side-note">Evidence-grounded intelligence<br />Tenant: default</div>
    </aside>
    <main className="main">
      <header className="topbar"><div><div className="eyebrow">Competitor intelligence</div><h1>Research desk</h1></div><div className="identity"><span className="avatar">C</span><span>{userId || "console-user"}</span></div></header>
      <div className="grid">
        <section className="panel"><div className="panel-header"><h2>New research run</h2><span className="mode">{selectedMode}</span></div><div className="panel-body"><form className="form-grid" onSubmit={submit}>
          <label>Research brief<textarea value={brief} onChange={(event) => setBrief(event.target.value)} /></label>
          <div className="row"><label>Competitors<input value={competitors} onChange={(event) => setCompetitors(event.target.value)} /></label><label>Mode<select value={mode} onChange={(event) => setMode(event.target.value as Mode)}><option value="auto">Auto route</option><option value="investigation">Investigation</option><option value="monitor">Monitor</option><option value="verify">Verify</option></select></label></div>
          <label>Source URLs<input placeholder="Optional URLs separated by spaces" value={urls} onChange={(event) => setUrls(event.target.value)} /></label>
          <div className="row"><label>API key<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} /></label><label>User ID<input value={userId} onChange={(event) => setUserId(event.target.value)} /></label></div>
          <p className="hint">The backend validates mode compatibility, evidence citations, tenant access, and budgets.</p>
          {error && <div className="error">{error}</div>}
          <button className="primary" disabled={busy}>{busy ? "Running research..." : "Start research"}</button>
        </form></div></section>
        <section className="panel"><div className="panel-header"><h2>Run telemetry</h2>{runId && <span className="status ok">live</span>}</div><div className="panel-body">
          <div className="stats"><div className="stat"><div className="stat-value">{trace.length}</div><div className="stat-label">Trace events</div></div><div className="stat"><div className="stat-value">{claims.length || "—"}</div><div className="stat-label">Verified claims</div></div><div className="stat"><div className="stat-value">{report?.status ?? "—"}</div><div className="stat-label">Report status</div></div></div>
          <div className="trace" style={{ marginTop: 18 }}>{trace.length ? trace.map((item, index) => <div className="trace-row" key={`${item.event}-${index}`}><div className="trace-time">{item.at ? new Date(item.at).toLocaleTimeString() : "now"}</div><div><div className="trace-event">{item.event}</div><div className="trace-detail">{typeof item.task_kind === "string" ? item.task_kind : typeof item.content_hash === "string" ? item.content_hash : ""}</div></div></div>) : <div className="empty">Run telemetry appears here</div>}</div>
        </div></section>
        <section className="panel"><div className="panel-header"><h2>Verified report</h2>{report && <span className={`status ${report.status === "approved" ? "ok" : "warn"}`}>{report.status}</span>}</div><div className="panel-body">{report ? <><h2 style={{ marginTop: 0 }}>{report.title}</h2>{findings.map((finding) => <div className="finding" key={finding.finding_id}><div className="eyebrow">{finding.fact_or_inference}</div><p>{finding.statement}</p><div className="finding-meta"><span className="chip">Confidence {Math.round(finding.confidence * 100)}%</span><span className="chip">{finding.claim_ids.length} claims</span><span className="chip">{finding.evidence_ids.length} sources</span></div></div>)}{report.unknowns.length > 0 && <p className="hint" style={{ marginTop: 15 }}>Unknowns: {report.unknowns.join("; ")}</p>}<div className="actions" style={{ marginTop: 18 }}><button className="secondary" onClick={() => window.open(runId ? exportUrl(runId, "json", apiKey, userId) : "", "_blank")}>Export JSON</button><button className="secondary" onClick={() => window.open(runId ? exportUrl(runId, "markdown", apiKey, userId) : "", "_blank")}>Export Markdown</button><button className="secondary" onClick={() => window.open(runId ? exportUrl(runId, "bundle", apiKey, userId) : "", "_blank")}>Evidence bundle</button></div><div style={{ marginTop: 18 }}><label>Approval comment<textarea style={{ minHeight: 72 }} value={comment} onChange={(event) => setComment(event.target.value)} /></label><button className="primary" style={{ marginTop: 10 }} onClick={approve} disabled={approved}>{approved ? "Approved" : "Approve report"}</button></div></> : <div className="empty">A completed report will appear here</div>}</div></section>
        <section className="panel"><div className="panel-header"><h2>Evidence ledger</h2><span className="status">immutable</span></div><div className="panel-body"><div className="evidence-list">{evidence.length ? evidence.map((item) => <article className="evidence" key={item.id}><div className="evidence-top"><a href={item.source_url} target="_blank" rel="noreferrer">{item.publisher || item.source_url}</a><span className="chip">{Math.round(item.credibility * 100)}%</span></div><p>{item.excerpt}</p></article>) : <div className="empty">Evidence appears after a run completes</div>}</div></div></section>
      </div>
    </main>
  </div>;
}
