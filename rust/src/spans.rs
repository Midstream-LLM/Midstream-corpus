use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap};
use std::fs::{self, File};
use std::io::{BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use pyo3::{prelude::*, types::PyBytes};
use rayon::prelude::*;

use super::{copy_row, mix, CopyInput, Text, BASE};

const K: usize = 32;
const W: usize = 32;
const MIN_SPAN: usize = 64;
const SORT_BYTES: usize = 64 << 20;
const TEXT_BYTES: usize = 256 << 20;
type Key = (u64, i64, u32);

fn write_record<const N: usize>(out: &mut impl Write, row: &[u32; N]) {
    for value in row {
        out.write_all(&value.to_le_bytes()).unwrap();
    }
}

fn read_record<const N: usize>(input: &mut impl Read) -> Option<[u32; N]> {
    let mut row = [0; N];
    let mut word = [0; 4];
    if input.read(&mut word).unwrap() == 0 {
        return None;
    }
    row[0] = u32::from_le_bytes(word);
    for value in &mut row[1..] {
        input.read_exact(&mut word).unwrap();
        *value = u32::from_le_bytes(word);
    }
    Some(row)
}

// 固定 64 MiB 排序块、64 路归并；偏斜分区也不整体读入内存。
struct Merge<const N: usize> {
    readers: Vec<BufReader<File>>,
    heap: BinaryHeap<Reverse<(Key, [u32; N], usize)>>,
    key: fn(&[u32; N]) -> Key,
    paths: Vec<PathBuf>,
}

impl<const N: usize> Merge<N> {
    fn new(paths: Vec<PathBuf>, key: fn(&[u32; N]) -> Key) -> Self {
        let readers = paths
            .iter()
            .map(|p| BufReader::new(File::open(p).unwrap()))
            .collect();
        let mut state = Self {
            readers,
            heap: BinaryHeap::new(),
            key,
            paths,
        };
        for (i, reader) in state.readers.iter_mut().enumerate() {
            if let Some(row) = read_record(reader) {
                state.heap.push(Reverse((key(&row), row, i)));
            }
        }
        state
    }
}

impl<const N: usize> Iterator for Merge<N> {
    type Item = [u32; N];

    fn next(&mut self) -> Option<Self::Item> {
        let Reverse((_, row, i)) = self.heap.pop()?;
        if let Some(next) = read_record(&mut self.readers[i]) {
            self.heap.push(Reverse(((self.key)(&next), next, i)));
        }
        Some(row)
    }
}

impl<const N: usize> Drop for Merge<N> {
    fn drop(&mut self) {
        for path in &self.paths {
            fs::remove_file(path).unwrap();
        }
    }
}

fn sorted<const N: usize>(
    path: &Path,
    key: fn(&[u32; N]) -> Key,
) -> Box<dyn Iterator<Item = [u32; N]>> {
    let in_memory = fs::metadata(path).unwrap().len() <= SORT_BYTES as u64;
    let mut input = BufReader::new(File::open(path).unwrap());
    let mut paths = Vec::new();
    let mut number = 0;
    loop {
        let mut bytes = Vec::new();
        input
            .by_ref()
            .take((SORT_BYTES / (N * 4) * (N * 4)) as u64)
            .read_to_end(&mut bytes)
            .unwrap();
        let mut rows: Vec<[u32; N]> = bytes
            .chunks_exact(N * 4)
            .map(|bytes| {
                std::array::from_fn(|i| {
                    u32::from_le_bytes(bytes[i * 4..i * 4 + 4].try_into().unwrap())
                })
            })
            .collect();
        drop(bytes);
        if rows.is_empty() {
            break;
        }
        rows.par_sort_unstable_by_key(|row| (key(row), *row));
        if in_memory {
            fs::remove_file(path).unwrap();
            return Box::new(rows.into_iter());
        }
        let run = path.with_extension(format!("run{number}"));
        number += 1;
        let mut out = BufWriter::new(File::create(&run).unwrap());
        for row in rows {
            write_record(&mut out, &row);
        }
        out.flush().unwrap();
        paths.push(run);
    }
    fs::remove_file(path).unwrap();
    while paths.len() > 64 {
        let mut next = Vec::new();
        for chunk in paths.chunks(64) {
            let run = path.with_extension(format!("run{number}"));
            number += 1;
            let mut out = BufWriter::new(File::create(&run).unwrap());
            for row in Merge::new(chunk.to_vec(), key) {
                write_record(&mut out, &row);
            }
            out.flush().unwrap();
            next.push(run);
        }
        paths = next;
    }
    Box::new(Merge::new(paths, key))
}

struct Partitions(Vec<Mutex<BufWriter<File>>>);

impl Partitions {
    fn new(dir: &Path, name: &str) -> Self {
        Self(
            (0..256)
                .map(|i| {
                    Mutex::new(BufWriter::new(
                        File::create(dir.join(format!("{name}{i}"))).unwrap(),
                    ))
                })
                .collect(),
        )
    }

    fn finish(self) {
        for file in self.0 {
            file.into_inner().unwrap().flush().unwrap();
        }
    }
}

// 每个 Rayon worker 最多缓存 256 × 4 KiB，按块加锁写盘。
struct Writer<'a> {
    partitions: &'a Partitions,
    buffers: Vec<Vec<u8>>,
}

impl<'a> Writer<'a> {
    fn new(partitions: &'a Partitions) -> Self {
        Self {
            partitions,
            buffers: vec![Vec::new(); 256],
        }
    }

    fn push<const N: usize>(&mut self, partition: usize, row: &[u32; N]) {
        write_record(&mut self.buffers[partition], row);
        if self.buffers[partition].len() >= 4096 {
            self.flush(partition);
        }
    }

    fn flush(&mut self, i: usize) {
        if !self.buffers[i].is_empty() {
            self.partitions.0[i]
                .lock()
                .unwrap()
                .write_all(&self.buffers[i])
                .unwrap();
            self.buffers[i].clear();
        }
    }
}

impl Drop for Writer<'_> {
    fn drop(&mut self) {
        for i in 0..256 {
            self.flush(i);
        }
    }
}

fn winnow(chars: &[u32], mut visit: impl FnMut(u64, u32)) {
    let mut queue = std::collections::VecDeque::with_capacity(W + 1);
    let (mut hash, mut selected) = (0u64, usize::MAX);
    let power = BASE.wrapping_pow((K - 1) as u32);
    for (i, &c) in chars.iter().enumerate() {
        if i >= K {
            hash = hash.wrapping_sub((chars[i - K] as u64).wrapping_mul(power));
        }
        hash = hash.wrapping_mul(BASE).wrapping_add(c as u64);
        if i + 1 < K {
            continue;
        }
        let pos = i + 1 - K;
        let value = mix(hash);
        while queue.back().is_some_and(|&(h, _)| h >= value) {
            queue.pop_back();
        }
        queue.push_back((value, pos));
        while queue.front().is_some_and(|&(_, p)| p + W <= pos) {
            queue.pop_front();
        }
        if pos + 1 >= W {
            let &(h, p) = queue.front().unwrap();
            if p != selected {
                visit(h, p as u32);
                selected = p;
            }
        }
    }
}

struct Document {
    version: i64,
    offset: u64,
    size: usize,
    bytes: usize,
}

struct Normalized {
    chars: Vec<u32>,
    positions: Vec<u32>,
}

#[pyclass]
pub struct SpanDeduper {
    dir: PathBuf,
    input: CopyInput,
    documents: Vec<Document>,
    blobs: Option<BufWriter<File>>,
    partitions: Option<Partitions>,
    fingerprint_count: usize,
    eligible: usize,
    output: Option<BufReader<File>>,
}

#[pymethods]
impl SpanDeduper {
    #[new]
    fn new(directory: String) -> Self {
        let dir = PathBuf::from(directory);
        Self {
            blobs: Some(BufWriter::new(File::create(dir.join("blobs")).unwrap())),
            partitions: Some(Partitions::new(&dir, "fingerprint")),
            dir,
            input: CopyInput::default(),
            documents: Vec::new(),
            fingerprint_count: 0,
            eligible: 0,
            output: None,
        }
    }

    fn fingerprints(&mut self, py: Python<'_>, data: &[u8]) {
        let rows = self.input.read::<2>(data);
        py.allow_threads(|| {
            let first = self.documents.len();
            let mut offset = self
                .documents
                .last()
                .map_or(0, |d| d.offset + d.size as u64);
            for [version, blob] in &rows {
                self.blobs.as_mut().unwrap().write_all(blob).unwrap();
                self.documents.push(Document {
                    version: i64::from_be_bytes((*version).try_into().unwrap()),
                    offset,
                    size: blob.len(),
                    bytes: zstd::zstd_safe::get_frame_content_size(blob)
                        .unwrap()
                        .unwrap() as usize,
                });
                offset += blob.len() as u64;
            }
            let mut start = 0;
            while start < rows.len() {
                let (mut end, mut bytes) = (start, 0);
                while end < rows.len() {
                    let extra = self.documents[first + end].bytes * 10;
                    if end > start && bytes + extra > TEXT_BYTES {
                        break;
                    }
                    bytes += extra;
                    end += 1;
                }
                let (fingerprints, eligible) = rows[start..end]
                    .par_iter()
                    .enumerate()
                    .map_init(
                        || (Text::new(), Writer::new(self.partitions.as_ref().unwrap())),
                        |(text, writer), (i, fields)| {
                            text.normalize_span(fields[1], false);
                            if text.chars.len() < MIN_SPAN {
                                return (0, 0);
                            }
                            let mut count = 0;
                            winnow(&text.chars, |hash, pos| {
                                writer.push(
                                    (hash >> 56) as usize,
                                    &[
                                        hash as u32,
                                        (hash >> 32) as u32,
                                        (first + start + i) as u32,
                                        pos,
                                    ],
                                );
                                count += 1;
                            });
                            (count, 1)
                        },
                    )
                    .reduce(|| (0, 0), |a, b| (a.0 + b.0, a.1 + b.1));
                self.fingerprint_count += fingerprints;
                self.eligible += eligible;
                start = end;
            }
        });
    }

    fn counts(&self) -> (usize, usize, usize) {
        (self.documents.len(), self.eligible, self.fingerprint_count)
    }

    fn seeds(&mut self, py: Python<'_>) -> (usize, usize) {
        py.allow_threads(|| {
            self.partitions.take().unwrap().finish();
            self.blobs.take().unwrap().flush().unwrap();
            self.input = CopyInput::default();
            let partitions = Partitions::new(&self.dir, "seed");
            let (mut count, mut templates) = (0, 0);
            let documents = &self.documents;
            for partition in 0..256 {
                let mut input = sorted(
                    &self.dir.join(format!("fingerprint{partition}")),
                    |r: &[u32; 4]| ((r[1] as u64) << 32 | r[0] as u64, r[2] as i64, r[3]),
                )
                .peekable();
                while input.peek().is_some() {
                    let mut groups = Vec::new();
                    let mut size = 0;
                    while size < 65536 && input.peek().is_some() {
                        let first = input.next().unwrap();
                        let mut group = vec![first];
                        let mut occurrences = 1;
                        while input.peek().is_some_and(|r| r[..2] == first[..2]) {
                            let row = input.next().unwrap();
                            occurrences += 1;
                            if occurrences <= 1000 {
                                group.push(row);
                            }
                        }
                        if occurrences > 1000 {
                            templates += 1;
                            continue;
                        }
                        size += group.len();
                        if group[0][2] != group.last().unwrap()[2] {
                            groups.push(group);
                        }
                    }
                    count += groups
                        .par_iter()
                        .map_init(
                            || Writer::new(&partitions),
                            |writer, group| {
                                // 星形配对：组按文档序排好，每篇只与最早出现这个指纹的一篇比较。
                                // 输出每段本来只记一个出处（较早的一篇），两两配对只会多出无用的候选。
                                let first = &group[0];
                                let mut count = 0;
                                for b in &group[1..] {
                                    if b[2] == first[2] {
                                        continue;
                                    }
                                    // 围绕解压成本较大的文档聚集候选，与版本方向无关。
                                    let (a, b) = if documents[first[2] as usize].bytes
                                        >= documents[b[2] as usize].bytes
                                    {
                                        (first, b)
                                    } else {
                                        (b, first)
                                    };
                                    writer.push(
                                        a[2] as usize * 256 / documents.len(),
                                        &[a[2], b[2], a[3], b[3]],
                                    );
                                    count += 1;
                                }
                                count
                            },
                        )
                        .sum::<usize>();
                }
            }
            partitions.finish();
            (count, templates)
        })
    }

    fn verify(&mut self, py: Python<'_>) -> (usize, usize) {
        py.allow_threads(|| self.verify_all())
    }

    fn next_output<'py>(&mut self, py: Python<'py>, run_id: i64) -> Option<Bound<'py, PyBytes>> {
        let mut output = Vec::new();
        while output.len() < 1 << 20 {
            let Some([b, start, end, a, source_start]) = read_record(self.output.as_mut().unwrap())
            else {
                break;
            };
            copy_row(
                &mut output,
                &[
                    &run_id.to_be_bytes(),
                    &self.documents[b as usize].version.to_be_bytes(),
                    &(start as i32).to_be_bytes(),
                    &(end as i32).to_be_bytes(),
                    &self.documents[a as usize].version.to_be_bytes(),
                    &(source_start as i32).to_be_bytes(),
                ],
            );
        }
        if output.is_empty() {
            None
        } else {
            Some(PyBytes::new(py, &output))
        }
    }
}

impl SpanDeduper {
    fn verify_all(&mut self) -> (usize, usize) {
        let mut output = BufWriter::new(File::create(self.dir.join("output")).unwrap());
        let (mut matches, mut spans) = (0, 0);
        let matched = Partitions::new(&self.dir, "match");
        for partition in 0..256 {
            let mut seeds = sorted(
                &self.dir.join(format!("seed{partition}")),
                |r: &[u32; 4]| {
                    (
                        (r[0] as u64) << 32 | r[1] as u64,
                        r[2] as i64 - r[3] as i64,
                        r[2],
                    )
                },
            )
            .peekable();
            // 种子按较大的文档聚集。它常是许多文档的共同出处（星形配对下更是如此），
            // 单篇就可能超过批次预算：不计入预算，并在相邻批次间沿用，避免为每个配对重复解压。
            let mut carried: Option<(u32, Arc<Normalized>)> = None;
            while seeds.peek().is_some() {
                let mut batch = Vec::new();
                let mut slots = HashMap::new();
                let mut bytes = 0;
                let anchor = seeds.peek().unwrap()[0];
                while let Some(&row) = seeds.peek() {
                    let extra: usize = row[..2]
                        .iter()
                        .filter(|doc| !slots.contains_key(*doc) && **doc != anchor)
                        .map(|&doc| {
                            self.documents[doc as usize].bytes * 18
                                + self.documents[doc as usize].size
                        })
                        .sum();
                    // 只在要加载新文档时检查预算：已装入的一对超出预算时，同一对的其余种子仍归这一批，
                    // 否则每条种子单独成批，两篇大文档要反复解压。
                    if !batch.is_empty()
                        && ((extra > 0 && bytes + extra > TEXT_BYTES) || batch.len() * 16 >= 16 << 20)
                    {
                        break;
                    }
                    bytes += extra;
                    for &doc in &row[..2] {
                        let slot = slots.len();
                        slots.entry(doc).or_insert(slot);
                    }
                    batch.push(seeds.next().unwrap());
                }
                let mut ids = vec![0; slots.len()];
                for (&doc, &slot) in &slots {
                    ids[slot] = doc;
                }
                let reuse = carried.take().filter(|(doc, _)| slots.contains_key(doc));
                let texts: Vec<Arc<Normalized>> = ids
                    .par_iter()
                    .map_init(
                        || {
                            (
                                File::open(self.dir.join("blobs")).unwrap(),
                                Text::new(),
                                Vec::new(),
                            )
                        },
                        |(file, text, blob), &doc| {
                            if let Some((kept, normalized)) = &reuse {
                                if *kept == doc {
                                    return normalized.clone();
                                }
                            }
                            let d = &self.documents[doc as usize];
                            file.seek(SeekFrom::Start(d.offset)).unwrap();
                            blob.resize(d.size, 0);
                            file.read_exact(blob).unwrap();
                            text.normalize_span(blob, true);
                            Arc::new(Normalized {
                                chars: std::mem::take(&mut text.chars),
                                positions: std::mem::take(&mut text.positions),
                            })
                        },
                    )
                    .collect();
                drop(reuse);
                // 同一对、同一对角线连续处理；已扩展区间内的种子直接合并，避免重复扩展。
                let mut starts = vec![0];
                for i in 1..batch.len() {
                    if batch[i][..2] != batch[i - 1][..2]
                        || batch[i][2] as i64 - batch[i][3] as i64
                            != batch[i - 1][2] as i64 - batch[i - 1][3] as i64
                    {
                        starts.push(i);
                    }
                }
                starts.push(batch.len());
                matches += starts
                    .par_windows(2)
                    .map_init(
                        || Writer::new(&matched),
                        |writer, range| {
                            let rows = &batch[range[0]..range[1]];
                            let [a, b, _, _] = rows[0];
                            let left = &texts[slots[&a]];
                            let right = &texts[slots[&b]];
                            let mut covered = 0;
                            let mut count = 0;
                            for &[_, _, pa, pb] in rows {
                                let (pa, pb) = (pa as usize, pb as usize);
                                if pa + K <= covered {
                                    continue;
                                }
                                if left.chars[pa..pa + K] != right.chars[pb..pb + K] {
                                    continue;
                                }
                                let (mut la, mut lb, mut end) = (pa, pb, pa + K);
                                while la > 0 && lb > 0 && left.chars[la - 1] == right.chars[lb - 1]
                                {
                                    la -= 1;
                                    lb -= 1;
                                }
                                while end < left.chars.len()
                                    && pb + end - pa < right.chars.len()
                                    && left.chars[end] == right.chars[pb + end - pa]
                                {
                                    end += 1;
                                }
                                covered = end;
                                if end - la >= MIN_SPAN {
                                    let row = if a < b {
                                        [
                                            b,
                                            right.positions[lb],
                                            right.positions[pb + end - pa - 1] + 1,
                                            a,
                                            left.positions[la],
                                        ]
                                    } else {
                                        [
                                            a,
                                            left.positions[la],
                                            left.positions[end - 1] + 1,
                                            b,
                                            right.positions[lb],
                                        ]
                                    };
                                    writer.push((mix(row[0] as u64) >> 56) as usize, &row);
                                    count += 1;
                                }
                            }
                            count
                        },
                    )
                    .sum::<usize>();
                let last = batch.last().unwrap()[0];
                carried = Some((last, texts[slots[&last]].clone()));
            }
        }
        matched.finish();
        for partition in 0..256 {
            let path = self.dir.join(format!("match{partition}"));
            // 同一起点最长者优先，删除被完整覆盖的区间；交叠且出处不同的区间保留证明。
            let mut document = u32::MAX;
            let mut covered = 0;
            for row in sorted(&path, |r: &[u32; 5]| {
                (r[0] as u64, r[1] as i64, u32::MAX - r[2])
            }) {
                if row[0] != document {
                    document = row[0];
                    covered = 0;
                }
                if row[2] > covered {
                    covered = row[2];
                    write_record(&mut output, &row);
                    spans += 1;
                }
            }
        }
        output.flush().unwrap();
        drop(output);
        self.output = Some(BufReader::new(File::open(self.dir.join("output")).unwrap()));
        (matches, spans)
    }
}

fn kgrams(chars: &[u32], mut visit: impl FnMut(u64, u32)) {
    let power = BASE.wrapping_pow((K - 1) as u32);
    let mut hash = 0u64;
    for (i, &c) in chars.iter().enumerate() {
        if i >= K {
            hash = hash.wrapping_sub((chars[i - K] as u64).wrapping_mul(power));
        }
        hash = hash.wrapping_mul(BASE).wrapping_add(c as u64);
        if i + 1 >= K {
            visit(mix(hash), (i + 1 - K) as u32);
        }
    }
}

fn merge_hits(mut a: HashMap<u64, i64>, b: HashMap<u64, i64>) -> HashMap<u64, i64> {
    for (hash, version) in b {
        let slot = a.entry(hash).or_insert(version);
        *slot = (*slot).min(version);
    }
    a
}

/// 净增量预估：样本和库内正文都取全部 K 字片段，命中的窗口连起来就是共有区间。
/// 每个片段记最小的库内版本号，结果与扫描顺序无关。
#[pyclass]
pub struct CoverageProbe {
    input: CopyInput,
    positions: Vec<Vec<u32>>,
    heads: HashMap<u64, u32>,
    entries: Vec<(u32, u32, u32)>,
    hits: HashMap<u64, i64>,
}

#[pymethods]
impl CoverageProbe {
    #[new]
    fn new() -> Self {
        Self {
            input: CopyInput::default(),
            positions: Vec::new(),
            heads: HashMap::new(),
            entries: Vec::new(),
            hits: HashMap::new(),
        }
    }

    fn add(&mut self, text: &str) {
        let (mut chars, mut positions) = (Vec::new(), Vec::new());
        for (position, c) in text.chars().enumerate() {
            let mapped = super::compare_char(c);
            if mapped != 0 {
                chars.push(mapped);
                positions.push(position as u32);
            }
        }
        let sample = self.positions.len() as u32;
        kgrams(&chars, |hash, pos| {
            let next = self
                .heads
                .insert(hash, self.entries.len() as u32)
                .unwrap_or(u32::MAX);
            self.entries.push((sample, pos, next));
        });
        self.positions.push(positions);
    }

    fn scan(&mut self, py: Python<'_>, data: &[u8]) {
        let rows = self.input.read::<2>(data);
        let heads = &self.heads;
        let found: HashMap<u64, i64> = py.allow_threads(|| {
            rows.par_iter()
                .fold(
                    || (Text::new(), HashMap::new()),
                    |(mut text, mut found), fields| {
                        let version = i64::from_be_bytes(fields[0].try_into().unwrap());
                        text.normalize_span(fields[1], false);
                        kgrams(&text.chars, |hash, _| {
                            if heads.contains_key(&hash) {
                                let slot = found.entry(hash).or_insert(version);
                                *slot = (*slot).min(version);
                            }
                        });
                        (text, found)
                    },
                )
                .map(|(_, found)| found)
                .reduce(HashMap::new, merge_hits)
        });
        self.hits = merge_hits(std::mem::take(&mut self.hits), found);
    }

    fn counts(&self) -> (usize, usize, usize) {
        (self.positions.len(), self.entries.len(), self.hits.len())
    }

    /// 每个样本：(原文中被覆盖的字数, [(库内版本, 覆盖的规范字数)])；只计不短于 MIN_SPAN 的区间。
    fn result(&self) -> Vec<(u64, Vec<(i64, u64)>)> {
        let mut covered: Vec<Vec<i64>> = self.positions.iter().map(|p| vec![0; p.len()]).collect();
        let mut hits: Vec<_> = self.hits.iter().collect();
        hits.sort();
        for (hash, &version) in hits {
            let mut i = self.heads[hash];
            while i != u32::MAX {
                let (sample, pos, next) = self.entries[i as usize];
                let pos = pos as usize;
                for slot in &mut covered[sample as usize][pos..pos + K] {
                    if *slot == 0 {
                        *slot = version;
                    }
                }
                i = next;
            }
        }
        covered
            .iter()
            .zip(&self.positions)
            .map(|(slots, positions)| {
                let (mut raw, mut by) = (0u64, HashMap::new());
                let mut start = 0;
                while start < slots.len() {
                    if slots[start] == 0 {
                        start += 1;
                        continue;
                    }
                    let mut end = start;
                    while end < slots.len() && slots[end] != 0 {
                        end += 1;
                    }
                    if end - start >= MIN_SPAN {
                        raw += (positions[end - 1] - positions[start] + 1) as u64;
                        for &version in &slots[start..end] {
                            *by.entry(version).or_insert(0u64) += 1;
                        }
                    }
                    start = end;
                }
                let mut by: Vec<_> = by.into_iter().collect();
                by.sort();
                (raw, by)
            })
            .collect()
    }
}
