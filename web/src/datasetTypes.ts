export type Filter = {
  sources?: string[]; authors?: string[]; categories?: string[]; genres?: string[]; quality?: string[];
  composed_label?: string; year_from?: number; year_to?: number;
  include_unknown_years?: boolean; only_unknown_years?: boolean;
  [key: string]: unknown;
};
export type Reservation = { doc_ids?: number[]; releases?: { release_id: number; split: string }[] };
export type Recipe = {
  name: string; seed: string; filters?: Filter; include?: Filter[]; exclude?: Filter[];
  documents?: { add?: number[]; remove?: number[] };
  selection?: { dedup?: string; source_priority?: string[] };
  splits: Record<string, number>; split_by?: string; stratify?: string[];
  limits?: Record<string, { max_chars?: number; max_documents?: number }>;
  reserved?: Record<string, Reservation>;
  variant?: "simplified";
};
export type Facet = { value: string | null; documents: number; chars: number };
export type Facets = Record<string, Facet[]>;
export type TokenStats = {
  tokenizer_name: string; tokenizer_fingerprint: string; total: number | null;
  status?: string; exact?: number | null; estimated?: number | null; coverage?: number;
  by_split?: Record<string, number>; percentiles?: Record<string, number>;
};
export type Release = {
  release_id: number; name: string; created_at: string; recipe: Recipe; content_digest: string;
  char_count: number; train_char_count: number; val_char_count: number; test_char_count: number;
  token_stats: TokenStats[];
  splits: Record<string, { documents: number; chars: number }>;
};
export type Plan = {
  candidates: number; dropped_documents: number; dropped_chars: number; documents: number; chars: number;
  warnings: string[]; splits: Record<string, { documents: number; chars: number; ratio: number }>;
  distributions: Record<string, { value: string | number | null; split: string; documents: number; chars: number }[]>;
  token_stats?: TokenStats;
};
export const splitNames: Record<string, string> = { train: "训练", val: "验证", test: "测试" };
