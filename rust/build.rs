use std::{env, fs, path::Path};

fn main() {
    let mut table = vec![0u32; 0x33480];
    for c in 0..table.len() as u32 {
        if c < 128 && (c as u8).is_ascii_alphanumeric()
            || matches!(c, 0x3400..=0x4dbf | 0x4e00..=0x9fff | 0xf900..=0xfaff
                | 0x20000..=0x2a6df | 0x2a700..=0x2b73f | 0x2b740..=0x2b81f
                | 0x2b820..=0x2ceaf | 0x2ceb0..=0x2ebef | 0x2ebf0..=0x2ee5f
                | 0x2f800..=0x2fa1f | 0x30000..=0x3134f | 0x31350..=0x323af
                | 0x323b0..=0x3347f)
        {
            table[c as usize] = c;
        }
    }
    for line in include_str!("data/TSCharacters.txt")
        .lines()
        .filter(|s| !s.starts_with('#'))
    {
        let mut fields = line.split_whitespace();
        if let Some(source) = fields.next() {
            table[source.chars().next().unwrap() as usize] =
                fields.next().unwrap().chars().next().unwrap() as u32;
        }
    }
    let data: Vec<u8> = table.iter().flat_map(|c| c.to_le_bytes()).collect();
    fs::write(
        Path::new(&env::var("OUT_DIR").unwrap()).join("compare.bin"),
        data,
    )
    .unwrap();
    println!("cargo:rerun-if-changed=data/TSCharacters.txt");
}
