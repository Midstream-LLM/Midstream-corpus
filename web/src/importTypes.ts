import type { CurationConfig } from "./Curation";
export type Plan = {format: string; encoding: string; text_field: string; id_field: string; fields: Record<string, string>;
  constants?: Record<string, string>; path_pattern?: string; head_patterns?: string[]; trim_start?: string; trim_end?: string; ocr_engine?: string};
export type FileItem = {path: string; size_bytes: number; known: boolean; source: string | null; adapter: string | null;
  format: string; encoding: string; signature: string; fields: Record<string, {type: string; example: string}>;
  sample: string; plan: Plan | null; note: string; import_status: string; duplicate_of?: string;
  previous_batches: {batch_id: number; status: string; source: string}[]};
export type Group = {id: string; source: string; adapter: string; plan: Plan; files: FileItem[]; selected: string[];
  approval: {method: "manual" | "rule" | "llm"; model?: string; edited?: boolean; requested_by: string};
  suggestion?: {plan: Plan | null; note: string; model: string; requested_by: string}; json?: string};
export type Preview = {source: string; parser_version: string; objects: {path: string; size_bytes: number; sha256: string}[];
  fill_rates: {field: string; filled: number; total: number}[];
  samples: {file: string; position: string; source_key: string; metadata: Record<string, unknown>; error: string | null; text_preview: string}[]};
export type Prepared = {curation?: CurationConfig | null;groupId: string; source: string; plan: Plan | null; paths: string[]; preview: Preview;
  approval: Group["approval"]; actor: string; reason: string; token: string; result?: {batch_id: number; job_id: number}};
export type Scan = {scan_id: string; status: string; current: string; found: number; items: FileItem[]; next_offset: number; errors: string[]};
export type Draft = {curation?: CurationConfig | null;step: number; directory: string; selected: string[]; recursive: boolean; scanId: string; scanOffset: number;
  scanStatus: string; scanErrors: string[]; files: FileItem[]; groups: Group[]; prepared: Prepared[]; note: string};
export const emptyDraft = (): Draft => ({step: 1, directory: "", selected: [], recursive: true, scanId: "", scanOffset: 0,
  scanStatus: "", scanErrors: [], files: [], groups: [], prepared: [], note: ""});
export const statusLabels: Record<string, string> = {new: "新文件", changed: "内容已变化", imported: "已发布 · 默认跳过",
  pending: "已有待处理批次", unsupported: "需提取正文", skipped: "非正文 · 已跳过", duplicate: "本次内容重复", error: "读取失败"};
export function groupFiles(files: FileItem[]): Group[] {
  const groups = new Map<string, Group>();
  for (const file of files) {
    const parent = file.path.slice(0, file.path.lastIndexOf("/"));
    const key = JSON.stringify([file.source || parent, file.signature, file.plan, file.adapter]);
    let group = groups.get(key);
    if (!group) {
      group = {id: crypto.randomUUID(), source: file.source || parent.split("/").pop() || "custom",
        adapter: file.adapter || "", plan: file.plan || {format: file.format, encoding: file.encoding || "utf-8-sig", text_field: "", id_field: "", fields: {}},
        files: [], selected: [], approval: {method: file.known ? "rule" : "manual", requested_by: ""}};
      groups.set(key, group);
    }
    group.files.push(file);
    if (["new", "changed"].includes(file.import_status)) group.selected.push(file.path);
  }
  return [...groups.values()];
}
