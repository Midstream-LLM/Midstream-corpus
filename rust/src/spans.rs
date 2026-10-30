use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap};
use std::fs::{self, File};
use std::io::{BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

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
                                let mut count = 0;
                                for (i, a) in group.iter().enumerate() {
                                    for b in &group[i + 1..] {
                                        if a[2] == b[2] {
                                            continue;
                                        }
                                        // 文档按 version_id 递增输入；按目标文档分区，方便最终区间去重。
                                        writer.push(
                                            (mix(b[2] as u64) >> 56) as usize,
                                            &[b[2], a[2], a[3], b[3]],
                                        );
                                        count += 1;
                                    }
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
        for partition in 0..256 {
            let path = self.dir.join(format!("match{partition}"));
            let mut matched = BufWriter::new(File::create(&path).unwrap());
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
            while seeds.peek().is_some() {
                let mut batch = Vec::new();
                let mut slots = HashMap::new();
                let mut bytes = 0;
                while let Some(&row) = seeds.peek() {
                    let extra: usize = row[..2]
                        .iter()
                        .filter(|doc| !slots.contains_key(*doc))
                        .map(|&doc| {
                            self.documents[doc as usize].bytes * 18
                                + self.documents[doc as usize].size
                        })
                        .sum();
                    if !batch.is_empty()
                        && (bytes + extra > TEXT_BYTES || batch.len() * 16 >= 16 << 20)
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
                let texts: Vec<Normalized> = ids
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
                            let d = &self.documents[doc as usize];
                            file.seek(SeekFrom::Start(d.offset)).unwrap();
                            blob.resize(d.size, 0);
                            file.read_exact(blob).unwrap();
                            text.normalize_span(blob, true);
                            Normalized {
                                chars: std::mem::take(&mut text.chars),
                                positions: std::mem::take(&mut text.positions),
                            }
                        },
                    )
                    .collect();
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
                let matched = Mutex::new(&mut matched);
                matches += starts
                    .par_windows(2)
                    .map(|range| {
                        let rows = &batch[range[0]..range[1]];
                        let [b, a, _, _] = rows[0];
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
                            while la > 0 && lb > 0 && left.chars[la - 1] == right.chars[lb - 1] {
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
                                write_record(
                                    &mut **matched.lock().unwrap(),
                                    &[
                                        b,
                                        right.positions[lb],
                                        right.positions[pb + end - pa - 1] + 1,
                                        a,
                                        left.positions[la],
                                    ],
                                );
                                count += 1;
                            }
                        }
                        count
                    })
                    .sum::<usize>();
            }
            matched.flush().unwrap();
            drop(matched);
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
