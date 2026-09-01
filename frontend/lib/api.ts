export type Mode = "auto" | "investigation" | "monitor" | "verify";

export type TraceEvent = { at?: string; event: string; [key: string]: unknown };
export type Finding = {
  finding_id: string;
  statement: string;
  claim_ids: string[];
  evidence_ids: string[];
  fact_or_inference: "fact" | "inference";
  confidence: number;
  contradictions?: string[];
  unknowns?: string[];
};
export type Evidence = {
  id: string;
  source_url: string;
  publisher?: string | null;
  retrieved_at: string;
  content_hash: string;
  excerpt: string;
  locator: string;
  source_type: string;
  credibility: number;
};
export type Claim = {
  id: string;
  subject: string;
  predicate: string;
  value: string | number | boolean;
  unit?: string | null;
  evidence_ids: string[];
  confidence: number;
  fact_or_inference: "fact" | "inference";
};
export type Report = {
  report_id: string;
  title: string;
  findings: Finding[];
  unknowns: string[];
  recommendations: string[];
  evidence_ids: string[];
  status: "draft" | "approved" | "published";
};
export type ResearchResponse = { report: Report; trace: { run_id: string; events: TraceEvent[] }; evidence?: Evidence[] };

const baseUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8787";

export function apiHeaders(apiKey: string, userId: string): HeadersInit {
  return { "Content-Type": "application/json", "X-API-Key": apiKey, "X-User-Id": userId };
}

export async function createResearch(input: { brief: string; competitors: string[]; urls: string[]; mode: Mode }, apiKey: string, userId: string, idempotencyKey: string): Promise<ResearchResponse> {
  const response = await fetch(`${baseUrl}/v1/research-runs`, { method: "POST", headers: { ...apiHeaders(apiKey, userId), "Idempotency-Key": idempotencyKey }, body: JSON.stringify(input) });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export async function getRun(runId: string, apiKey: string, userId: string): Promise<ResearchResponse & { status: string }> {
  const response = await fetch(`${baseUrl}/v1/research-runs/${runId}`, { headers: apiHeaders(apiKey, userId), cache: "no-store" });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export async function actionReport(reportId: string, comment: string, apiKey: string, userId: string): Promise<void> {
  const response = await fetch(`${baseUrl}/v1/reports/${reportId}/approve`, { method: "POST", headers: apiHeaders(apiKey, userId), body: JSON.stringify({ comment }) });
  if (!response.ok) throw new Error(await response.text());
}

export function exportUrl(runId: string, format: "json" | "markdown" | "bundle", apiKey: string, userId: string): string {
  const params = new URLSearchParams({ format, api_key: apiKey, user_id: userId });
  return `${baseUrl}/v1/research-runs/${runId}/export?${params}`;
}
