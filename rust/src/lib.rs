use std::collections::{HashMap, HashSet, VecDeque};

use pyo3::{prelude::*, types::PyBytes};
use rayon::prelude::*;
use xxhash_rust::xxh3::{xxh3_128, xxh3_64};

mod spans;

const TABLE: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/compare.bin"));
const WIDTH: usize = 128 * 4;
const BASE: u64 = 0x100000001b3;

struct Text {
    decoder: zstd::bulk::Decompressor<'static>,
    raw: Vec<u8>,
    utf8: Vec<u8>,
    chars: Vec<u32>,
    positions: Vec<u32>,
}

impl Text {
    fn new() -> Self {
        Self {
            decoder: zstd::bulk::Decompressor::new().unwrap(),
            raw: Vec::new(),
            utf8: Vec::new(),
            chars: Vec::new(),
            positions: Vec::new(),
        }
    }

    fn normalize(&mut self, blob: &[u8]) {
        self.decode(blob, false, true);
    }

    fn normalize_span(&mut self, blob: &[u8], positions: bool) {
        self.decode(blob, positions, false);
    }

    fn decode(&mut self, blob: &[u8], positions: bool, utf8: bool) {
        let size = zstd::zstd_safe::get_frame_content_size(blob)
            .unwrap()
            .unwrap() as usize;
        self.raw.resize(size, 0);
        self.decoder
            .decompress_to_buffer(blob, self.raw.as_mut_slice())
            .unwrap();
        self.utf8.clear();
        self.chars.clear();
        self.positions.clear();
        for (position, c) in std::str::from_utf8(&self.raw).unwrap().chars().enumerate() {
            let offset = c as usize * 4;
            if offset < TABLE.len() {
                let mapped = u32::from_le_bytes(TABLE[offset..offset + 4].try_into().unwrap());
                if mapped != 0 {
                    self.chars.push(mapped);
                    if positions {
                        self.positions.push(position as u32);
                    }
                    if utf8 {
                        let mut buf = [0; 4];
                        self.utf8.extend_from_slice(
                            char::from_u32(mapped)
                                .unwrap()
                                .encode_utf8(&mut buf)
                                .as_bytes(),
                        );
                    }
                }
            }
        }
    }
}

fn mix(mut h: u64) -> u64 {
    h ^= h >> 30;
    h = h.wrapping_mul(0xbf58476d1ce4e5b9);
    h ^= h >> 27;
    h = h.wrapping_mul(0x94d049bb133111eb);
    h ^ (h >> 31)
}

fn grams(chars: &[u32], mut visit: impl FnMut(u64)) {
    let mut h = 0u64;
    let power = BASE.wrapping_pow(7);
    for (i, &c) in chars.iter().enumerate() {
        if i >= 8 {
            h = h.wrapping_sub((chars[i - 8] as u64).wrapping_mul(power));
        }
        h = h.wrapping_mul(BASE).wrapping_add(c as u64);
        if i >= 7 {
            visit(mix(h));
        }
    }
}

fn signature(chars: &[u32]) -> [u32; 128] {
    if chars.len() < 8 {
        return [0; 128];
    }
    let mut bins = [u32::MAX; 128];
    grams(chars, |h| {
        let bucket = (h & 127) as usize;
        bins[bucket] = bins[bucket].min(((h >> 7) as u32) & 0x1ffffff);
    });
    // 循环向右借用非空桶，把距离编码进去以区分来源。
    let first = bins.iter().position(|&v| v != u32::MAX).unwrap();
    let mut value = bins[first];
    let mut distance = 0;
    for step in 1..128 {
        let i = (first + 128 - step) & 127;
        if bins[i] == u32::MAX {
            distance += 1;
            bins[i] = value | (distance << 25);
        } else {
            value = bins[i];
            distance = 0;
        }
    }
    bins
}

const COPY_HEADER: &[u8] = b"PGCOPY\n\xff\r\n\0\0\0\0\0\0\0\0\0";

#[derive(Default)]
struct CopyInput {
    buffer: Vec<u8>,
    position: usize,
    header: bool,
}

impl CopyInput {
    fn read<const N: usize>(&mut self, data: &[u8]) -> Vec<[&[u8]; N]> {
        self.buffer.drain(..self.position);
        self.position = 0;
        self.buffer.extend_from_slice(data);
        if !self.header {
            if self.buffer.len() < 19 {
                return Vec::new();
            }
            let extension = i32::from_be_bytes(self.buffer[15..19].try_into().unwrap()) as usize;
            if self.buffer.len() < 19 + extension {
                return Vec::new();
            }
            self.position = 19 + extension;
            self.header = true;
        }
        let mut rows = Vec::new();
        while self.position + 2 <= self.buffer.len() {
            if self.buffer[self.position..self.position + 2] == [255, 255] {
                self.position += 2;
                self.header = false;
                break;
            }
            let mut end = self.position + 2;
            let mut fields = [(0, 0); N];
            for field in &mut fields {
                if end + 4 > self.buffer.len() {
                    return rows;
                }
                let size =
                    i32::from_be_bytes(self.buffer[end..end + 4].try_into().unwrap()) as usize;
                end += 4;
                if end + size > self.buffer.len() {
                    return rows;
                }
                *field = (end, end + size);
                end += size;
            }
            rows.push(fields.map(|(start, end)| &self.buffer[start..end]));
            self.position = end;
        }
        rows
    }
}

fn copy_row(output: &mut Vec<u8>, fields: &[&[u8]]) {
    output.extend_from_slice(&(fields.len() as i16).to_be_bytes());
    for field in fields {
        output.extend_from_slice(&(field.len() as i32).to_be_bytes());
        output.extend_from_slice(field);
    }
}

struct Groups(Vec<u32>);

impl Groups {
    fn new(n: usize) -> Self {
        Self((0..n as u32).collect())
    }

    fn find(&mut self, mut i: u32) -> u32 {
        while self.0[i as usize] != i {
            self.0[i as usize] = self.0[self.0[i as usize] as usize];
            i = self.0[i as usize];
        }
        i
    }

    fn union(&mut self, a: u32, b: u32) {
        let a = self.find(a);
        let b = self.find(b);
        self.0[a.max(b) as usize] = a.min(b);
    }
}

struct Representative {
    version: i64,
    hash: [u8; 16],
    signature: [u8; WIDTH],
    versions: Vec<i64>,
}

#[pyclass]
struct Deduper {
    rule_version: String,
    bands: usize,
    rows: usize,
    threshold: f64,
    input: CopyInput,
    fingerprinted: usize,
    documents: usize,
    exact: HashMap<[u8; 16], usize>,
    representatives: Vec<Representative>,
    groups: Groups,
    buckets: Vec<Vec<u32>>,
    round: usize,
    tested: HashSet<(u32, u32)>,
    candidates: usize,
    verified: usize,
    pending: VecDeque<(u32, u32)>,
    batch: Vec<(usize, usize)>,
    nodes: Vec<u32>,
    blob_slots: HashMap<i64, usize>,
    blobs: Vec<Vec<u8>>,
    cached: HashMap<([u8; 16], [u8; 16]), f64>,
    cache_hits: usize,
    computed_pairs: usize,
    clusters: Vec<Vec<(i64, bool)>>,
    cluster_position: usize,
}

#[pymethods]
impl Deduper {
    #[new]
    fn new(rule_version: String, bands: usize, rows: usize, threshold: f64) -> Self {
        Self {
            rule_version,
            bands,
            rows,
            threshold,
            input: CopyInput::default(),
            fingerprinted: 0,
            documents: 0,
            exact: HashMap::new(),
            representatives: Vec::new(),
            groups: Groups::new(0),
            buckets: Vec::new(),
            round: 0,
            tested: HashSet::new(),
            candidates: 0,
            verified: 0,
            pending: VecDeque::new(),
            batch: Vec::new(),
            nodes: Vec::new(),
            blob_slots: HashMap::new(),
            blobs: Vec::new(),
            cached: HashMap::new(),
            cache_hits: 0,
            computed_pairs: 0,
            clusters: Vec::new(),
            cluster_position: 0,
        }
    }

    #[staticmethod]
    fn copy_header(py: Python<'_>) -> Bound<'_, PyBytes> {
        PyBytes::new(py, COPY_HEADER)
    }

    #[staticmethod]
    fn copy_end(py: Python<'_>) -> Bound<'_, PyBytes> {
        PyBytes::new(py, &[255, 255])
    }

    fn fingerprints<'py>(&mut self, py: Python<'py>, data: &[u8]) -> Bound<'py, PyBytes> {
        let rows = self.input.read::<2>(data);
        self.fingerprinted += rows.len();
        let rule = self.rule_version.as_bytes();
        let width = 2 + 4 * 4 + 32 + rule.len() + 16 + WIDTH;
        let output = PyBytes::new_with(py, rows.len() * width, |output| {
            py.allow_threads(|| {
                let chunk = rows.len().div_ceil(rayon::current_num_threads() * 2).max(1);
                output
                    .par_chunks_mut(width)
                    .zip(rows.par_iter())
                    .with_min_len(chunk)
                    .for_each_init(Text::new, |text, (out, fields)| {
                        text.normalize(fields[1]);
                        let mut sig = [0; WIDTH];
                        for (slot, value) in sig.chunks_exact_mut(4).zip(signature(&text.chars)) {
                            slot.copy_from_slice(&value.to_le_bytes());
                        }
                        let hash = xxh3_128(&text.utf8).to_le_bytes();
                        let mut position = 2;
                        out[..2].copy_from_slice(&4i16.to_be_bytes());
                        for field in [fields[0], rule, &hash, &sig] {
                            out[position..position + 4]
                                .copy_from_slice(&(field.len() as i32).to_be_bytes());
                            position += 4;
                            out[position..position + field.len()].copy_from_slice(field);
                            position += field.len();
                        }
                    });
            });
            Ok(())
        })
        .unwrap();
        output
    }

    #[getter]
    fn fingerprinted(&self) -> usize {
        self.fingerprinted
    }

    fn versions(&mut self, py: Python<'_>, data: &[u8]) {
        let rows = self.input.read::<3>(data);
        py.allow_threads(|| {
            self.documents += rows.len();
            for [version, hash, sig] in rows {
                let version = i64::from_be_bytes(version.try_into().unwrap());
                let hash: [u8; 16] = hash.try_into().unwrap();
                if let Some(&i) = self.exact.get(&hash) {
                    let rep = &mut self.representatives[i];
                    rep.version = rep.version.min(version);
                    rep.versions.push(version);
                } else {
                    self.exact.insert(hash, self.representatives.len());
                    self.representatives.push(Representative {
                        version,
                        hash,
                        signature: sig.try_into().unwrap(),
                        versions: vec![version],
                    });
                }
            }
        });
    }

    fn group_exact(&mut self, py: Python<'_>) -> (usize, usize, usize) {
        py.allow_threads(|| {
            self.exact.clear();
            self.exact.shrink_to_fit();
            self.representatives.sort_unstable_by_key(|rep| rep.version);
            self.groups = Groups::new(self.representatives.len());
            (
                self.documents,
                self.representatives.len(),
                self.representatives
                    .iter()
                    .filter(|rep| rep.versions.len() > 1)
                    .count(),
            )
        })
    }

    fn build_lsh(&mut self, py: Python<'_>) {
        py.allow_threads(|| {
            let active: Vec<u32> = self
                .representatives
                .iter()
                .enumerate()
                .filter(|(_, rep)| rep.signature.iter().any(|&v| v != 0))
                .map(|(i, _)| i as u32)
                .collect();
            self.buckets = (0..self.bands)
                .into_par_iter()
                .map(|band| {
                    let start = band * self.rows * 4;
                    let mut entries: Vec<_> = active
                        .iter()
                        .map(|&i| {
                            let sig = &self.representatives[i as usize].signature
                                [start..start + self.rows * 4];
                            (xxh3_64(sig), i)
                        })
                        .collect();
                    entries.sort_unstable();
                    let mut buckets = Vec::new();
                    let mut start = 0;
                    while start < entries.len() {
                        let mut end = start + 1;
                        while end < entries.len() && entries[end].0 == entries[start].0 {
                            end += 1;
                        }
                        if end - start > 1 {
                            buckets.push(entries[start..end].iter().map(|&(_, i)| i).collect());
                        }
                        start = end;
                    }
                    buckets
                })
                .collect::<Vec<Vec<Vec<u32>>>>()
                .into_iter()
                .flatten()
                .collect();
        });
    }

    fn next_round(&mut self, py: Python<'_>) -> bool {
        if self.round == 6 {
            return false;
        }
        py.allow_threads(|| {
            let mut fresh = false;
            for bucket in &self.buckets {
                let mut roots = HashSet::new();
                let nodes: Vec<u32> = bucket
                    .iter()
                    .copied()
                    .filter(|&i| roots.insert(self.groups.find(i)))
                    .collect();
                if nodes.len() < 2 {
                    continue;
                }
                // 轮换中心，避免未通过验证的中心一直占住桶。
                let a = nodes[self.round % nodes.len()];
                for &b in &nodes {
                    if a == b {
                        continue;
                    }
                    let pair = (a.min(b), a.max(b));
                    if self.tested.insert(pair) {
                        fresh = true;
                        let left = &self.representatives[a as usize].signature;
                        let right = &self.representatives[b as usize].signature;
                        let equal = left
                            .chunks_exact(4)
                            .zip(right.chunks_exact(4))
                            .filter(|(a, b)| a == b)
                            .count();
                        if equal as f64 / 128.0 >= 0.6 {
                            self.pending.push_back(pair);
                            self.candidates += 1;
                        }
                    }
                }
            }
            self.round += 1;
            fresh
        })
    }

    fn next_batch<'py>(&mut self, py: Python<'py>, limit: usize) -> Option<Bound<'py, PyBytes>> {
        if self.pending.is_empty() {
            return None;
        }
        self.nodes.clear();
        self.blob_slots.clear();
        self.batch.clear();
        self.cached.clear();
        let mut local = HashMap::new();
        while let Some(&(a, b)) = self.pending.front() {
            let extra = usize::from(!local.contains_key(&a)) + usize::from(!local.contains_key(&b));
            if !self.batch.is_empty() && local.len() + extra > limit {
                break;
            }
            self.pending.pop_front();
            let mut slots = [0; 2];
            for (slot, node) in slots.iter_mut().zip([a, b]) {
                *slot = *local.entry(node).or_insert_with(|| {
                    self.nodes.push(node);
                    self.nodes.len() - 1
                });
            }
            self.batch.push((slots[0], slots[1]));
        }
        self.blobs = vec![Vec::new(); self.nodes.len()];
        let mut output = COPY_HEADER.to_vec();
        for (slot, &node) in self.nodes.iter().enumerate() {
            let version = self.representatives[node as usize].version;
            self.blob_slots.insert(version, slot);
            copy_row(&mut output, &[&version.to_be_bytes()]);
        }
        output.extend_from_slice(&[255, 255]);
        Some(PyBytes::new(py, &output))
    }

    fn candidate_pairs<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        let mut output = COPY_HEADER.to_vec();
        for &(a, b) in &self.batch {
            let (a, b) = self.pair_hashes(a, b);
            copy_row(&mut output, &[&a, &b]);
        }
        output.extend_from_slice(&[255, 255]);
        PyBytes::new(py, &output)
    }

    fn cached_pairs(&mut self, data: &[u8]) {
        for [a, b, score] in self.input.read::<3>(data) {
            self.cached.insert(
                (a.try_into().unwrap(), b.try_into().unwrap()),
                f64::from_be_bytes(score.try_into().unwrap()),
            );
        }
    }

    fn uncached_batch<'py>(&mut self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        let mut needed = vec![false; self.nodes.len()];
        let pairs = std::mem::take(&mut self.batch);
        for (a, b) in pairs {
            if let Some(&score) = self.cached.get(&self.pair_hashes(a, b)) {
                self.cache_hits += 1;
                if score >= self.threshold {
                    self.verified += 1;
                    self.groups.union(self.nodes[a], self.nodes[b]);
                }
            } else {
                self.batch.push((a, b));
                needed[a] = true;
                needed[b] = true;
            }
        }
        if self.batch.is_empty() {
            return None;
        }
        let mut output = COPY_HEADER.to_vec();
        for (&node, needed) in self.nodes.iter().zip(needed) {
            if needed {
                copy_row(&mut output, &[&self.representatives[node as usize].version.to_be_bytes()]);
            }
        }
        output.extend_from_slice(&[255, 255]);
        Some(PyBytes::new(py, &output))
    }

    #[getter]
    fn cache_hits(&self) -> usize {
        self.cache_hits
    }

    #[getter]
    fn computed_pairs(&self) -> usize {
        self.computed_pairs
    }

    fn blobs(&mut self, data: &[u8]) {
        for [version, content] in self.input.read::<2>(data) {
            let version = i64::from_be_bytes(version.try_into().unwrap());
            self.blobs[self.blob_slots[&version]] = content.to_vec();
        }
    }

    fn verify_batch<'py>(&mut self, py: Python<'py>) -> Bound<'py, PyBytes> {
        let output = py.allow_threads(|| {
            let sets: Vec<Vec<u64>> = self
                .blobs
                .par_iter()
                .map_init(Text::new, |text, blob| {
                    // 已缓存的候选无需加载正文，保留空槽位以复用本批节点编号。
                    if blob.is_empty() {
                        return Vec::new();
                    }
                    text.normalize(blob);
                    let mut values = Vec::new();
                    grams(&text.chars, |h| values.push(h));
                    values.sort_unstable();
                    values.dedup();
                    values
                })
                .collect();
            let scores: Vec<f64> = self
                .batch
                .par_iter()
                .map(|&(a, b)| jaccard(&sets[a], &sets[b]))
                .collect();
            self.computed_pairs += scores.len();
            let mut output = COPY_HEADER.to_vec();
            for (&(a, b), score) in self.batch.iter().zip(scores) {
                let (left, right) = self.pair_hashes(a, b);
                copy_row(&mut output, &[&left, &right, &score.to_be_bytes()]);
                if score >= self.threshold {
                    self.verified += 1;
                    self.groups.union(self.nodes[a], self.nodes[b]);
                }
            }
            self.blobs.clear();
            output.extend_from_slice(&[255, 255]);
            output
        });
        PyBytes::new(py, &output)
    }

    fn clusters(&mut self, py: Python<'_>) -> (usize, usize, usize, usize) {
        py.allow_threads(|| {
            let mut clusters = vec![Vec::new(); self.representatives.len()];
            for (i, rep) in self.representatives.iter().enumerate() {
                let root = self.groups.find(i as u32) as usize;
                clusters[root].extend(rep.versions.iter().map(|&v| (v, i == root)));
            }
            self.clusters = clusters
                .into_iter()
                .filter(|members| members.len() > 1)
                .collect();
            (
                self.clusters.len(),
                self.clusters.iter().map(Vec::len).sum(),
                self.candidates,
                self.verified,
            )
        })
    }

    fn members<'py>(&mut self, py: Python<'py>, data: &[u8]) -> Bound<'py, PyBytes> {
        let mut output = Vec::new();
        for [cluster] in self.input.read::<1>(data) {
            for &(version, exact) in &self.clusters[self.cluster_position] {
                let kind: &[u8] = if exact { b"exact" } else { b"near_candidate" };
                copy_row(&mut output, &[cluster, &version.to_be_bytes(), kind]);
            }
            self.cluster_position += 1;
        }
        PyBytes::new(py, &output)
    }
}

impl Deduper {
    fn pair_hashes(&self, a: usize, b: usize) -> ([u8; 16], [u8; 16]) {
        let a = self.representatives[self.nodes[a] as usize].hash;
        let b = self.representatives[self.nodes[b] as usize].hash;
        (a.min(b), a.max(b))
    }
}

fn jaccard(a: &[u64], b: &[u64]) -> f64 {
    let (mut i, mut j, mut intersection) = (0, 0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                i += 1;
                j += 1;
                intersection += 1;
            }
        }
    }
    intersection as f64 / (a.len() + b.len() - intersection) as f64
}

#[pymodule]
fn midstream_dedup(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Deduper>()?;
    m.add_class::<spans::SpanDeduper>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn normalized(text: &str) -> Text {
        let mut state = Text::new();
        state.normalize(&zstd::bulk::compress(text.as_bytes(), 1).unwrap());
        state
    }

    #[test]
    fn identical_oph() {
        let a = normalized("黄帝者少典之子姓公孙名曰轩辕");
        let b = normalized("黄帝者少典之子姓公孙名曰轩辕");
        assert_eq!(signature(&a.chars), signature(&b.chars));
    }

    #[test]
    fn traditional_and_punctuation() {
        let a = normalized("齊高祖神武皇帝，姓高名歡。ABC123");
        let b = normalized("齐高祖神武皇帝 姓高名欢！ABC123");
        assert_eq!(xxh3_128(&a.utf8), xxh3_128(&b.utf8));
    }
}
