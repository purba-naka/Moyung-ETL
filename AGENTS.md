# AGENTS.md — excel-etl

WAJIB dibaca di awal setiap session sebelum menyentuh kode di `excel-etl/`.
Urutan baca: `AGENTS.md` (ini) -> `IMPLEMENTATION.md` (status + langkah berikut)
-> `DESIGN.md` (rujukan detail, baca bagian yang relevan saja).

## 1. Apa ini

Library Python murni: tools untuk agent LLM memahami Excel raw (banyak sheet,
banyak tabel, bentuk tak seragam), membangun pipeline ETL, menjalankannya, dan
me-recycle pipeline saat struktur Excel berubah. Output: data bersih
(parquet/csv) + laporan kualitas. Load ke Snowflake = fase berikut, bukan sekarang.

Pemilik: Gilbert (komponen). Rekan menyambungkan ke PINTER (arsitektur/agent loop).

## 2. Batas keras (jangan dilanggar)

- **Tanpa** import `google.adk`, `fastapi`, `litellm`, `sqlalchemy`, atau modul
  `pinter-be-refactor`. Host yang membungkus. Library harus jalan berdiri sendiri.
- **Bukan MCP server.** Permukaan publik = fungsi Python di `excel_etl/tools.py`.
- **Library tidak memblok untuk HITL.** `ask_human` mengembalikan `PendingQuestion`;
  host yang menyampaikan ke user.
- **Library tidak menyimpan state sendiri** kecuali lewat `StateStore` yang diberikan
  host (mode `store`) atau `FileStore` bawaan untuk dev/test. Lihat DESIGN §9.
- **File Excel tidak pernah masuk state.** Hanya `FileRef(id, filename, sha256)`.
- **Tidak ada `eval`/`exec`.** Ekspresi agent = SQL skalar duckdb yang divalidasi,
  `enable_external_access=false`.
- **Tidak commit/push** kecuali diminta eksplisit.
- **File contoh asli (BALANSITAS) jangan masuk repo** (data internal). Pakai via env
  `EXCEL_ETL_SAMPLE`; test unit pakai workbook sintetis buatan openpyxl.

## 3. Dependency

Hanya: `python-calamine`, `openpyxl`, `duckdb`, `pydantic>=2`. Dev: `pytest`, `ruff`.
Menambah dependency = riset dulu (alasan, ukuran, alternatif stdlib), catat di
`IMPLEMENTATION.md` §Keputusan. Ditolak & alasannya: `sheetwise`, `SheetBrain`,
`pandas` (lihat IMPLEMENTATION §Temuan).

## 4. Aturan teknis

**Membaca Excel**
- Nilai, merged, visibility: `python-calamine` (cepat). Default.
- Formula, style, number format, defined names: `openpyxl` `read_only=True`, **lazy
  per sheet**, hanya bila tool butuh. Cache berbasis `sha256`.
- Perilaku calamine 0.9.x (terverifikasi): sel kosong -> `""`; int -> float
  (pakai `is_integer()`); merged range 0-based inklusif; formula tanpa cached value
  -> `""` diam-diam.
- Basis deteksi region: port dari `pinter-be-refactor/modules/parse_excel/parser.py`
  (header bertingkat, merged vertikal, total row, multi-tabel). Copy & sesuaikan,
  jangan import.

**Tool untuk agent (kompatibel LiteLLM, semua provider)**
- Parameter: primitif, enum, list primitif. Tanpa `anyOf`/`oneOf`/`$ref`/objek
  bersarang. Struktur kompleks (spec) dikirim sebagai string YAML/JSON lalu divalidasi.
- Return: `dict` JSON-serializable. Teks ringkas di field `text`; selalu ada
  `truncated` + `next_offset` bila dipaginasi.
- Budget output default <= ~6k token per panggilan.
- Error dikembalikan sebagai `{"error": "...", "hint": "..."}`, bukan exception.
  Exception hanya untuk bug internal.
- ID & urutan deterministik: `F1`, `F1.S12`, `F1.S12.R2`.
- Docstring tool = deskripsi untuk LLM: singkat, sebut kapan dipakai, arti field output.
- Teks saja; vision opsional, tidak pernah wajib.

**Encoding untuk LLM** (DESIGN §4, IMPLEMENTATION §Temuan)
- Bertingkat: workbook_map -> sheet_regions -> render_range(compressed) -> render_range(raw).
- Kolom angka/tanggal diringkas jadi tipe + rentang (`<date 2026-01-01..2027-12-31>`),
  bukan didaftar per sel. Teks berulang -> inverted index. Header/batas -> anchor.
- Target: sheet 800x160 terisi < 1.000 token di mode compressed.

**Pipeline**
- Spec menyimpan **anchor semantik**, bukan alamat sel. Alamat hanya hasil bind.
- Setiap node punya `infer_schema` (validasi tanpa data) dan `run` (duckdb).
- Repair menghasilkan versi spec baru + diff; tidak menimpa.

## 5. Gaya kode

- Python 3.12, type hints penuh, `from __future__ import annotations`.
- Fewest files. Tanpa abstraksi untuk satu implementasi, tanpa factory, tanpa
  config untuk nilai yang tidak berubah. Registry node = dict + decorator `@node`.
- Penyederhanaan sengaja ditandai `# ponytail: <batas>; upgrade: <jalan>`.
- Komentar hanya untuk "kenapa", bukan "apa" (detail gaya: §8 P4).
- Nama publik stabil; perubahan nama tool = catat di IMPLEMENTATION §Log.

## 6. Test & verifikasi

Setiap logika non-trivial punya test. Sebelum menyatakan selesai:

```bash
# di excel-etl/
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
# opsional, butuh file asli:
EXCEL_ETL_SAMPLE="C:/Users/ggnpg/Downloads/(2026) BALANSITAS AMONIAK, UREA, NPK, DAN BAHAN BAKU 3.0.xlsx" uv run pytest -q -m sample
```

- Test sintetis: bangun workbook di memori dengan openpyxl (pola: lihat
  `pinter-be-refactor/tests/test_parse_excel.py`).
- Test `sample` (file asli) ditandai `@pytest.mark.sample`, di-skip bila env kosong.
- Test encoding wajib assert budget token (chars/4) agar regresi kompresi ketahuan.

## 7. Workflow session

1. Baca file ini, lalu `IMPLEMENTATION.md`: lihat **Status** dan **Berikutnya**.
2. Kerjakan satu item. Jangan lompat fase tanpa persetujuan user.
3. Sebelum koding fitur baru: jalankan **P1 Review plan** (§8) pada bagian
   `IMPLEMENTATION.md`/`DESIGN.md` yang terkait.
4. Setelah koding: jalankan **P2–P5** (§8) berurutan, lalu §6 verifikasi.
5. Setelah selesai: centang checklist, isi **Log** (tanggal, apa, keputusan), perbarui
   **Berikutnya** di `IMPLEMENTATION.md`.
6. Keputusan desain baru -> `IMPLEMENTATION.md` §Keputusan; bila mengubah desain,
   perbarui juga `DESIGN.md`.
7. Ragu soal requirement -> tanya user, jangan menebak.

## 8. Quality passes

Dijalankan sebagai pass terpisah (satu fokus per pass), bukan dicampur saat
menulis kode. "Perubahan terakhir" = diff item yang baru dikerjakan (atau commit
terakhir bila sudah di-commit). User bisa memanggil satu pass saja dengan nama
`P1`..`P8`.

**Prinsip yang dipakai semua pass**
- **Steelman tiap perubahan**: sebelum mengubah, tulis argumen terkuat untuk
  *mempertahankan* versi lama. Ubah hanya bila argumen perubahan lebih kuat.
  Catat singkat di ringkasan akhir: `ubah X: alasan; steelman lama: ...; menang karena ...`.
- **Jangan buang fungsi kritis**: validasi di batas trust, error handling yang
  mencegah data hilang, keamanan, apa pun yang diminta eksplisit.
- **Hapus kelas bug, bukan satu bug**: lebih pilih desain yang membuat bug tidak
  mungkin terjadi (tipe, invariant di satu tempat, state yang tak bisa
  inkonsisten) daripada tambalan `if`.
- **Single source of truth**: satu fakta (nama kolom, ID region, aturan tipe,
  daftar node) hanya didefinisikan di satu tempat; yang lain menurunkan darinya.
- **Ukuran "2 pagi setahun lagi"**: orang yang belum pernah lihat kode ini bisa
  menemukan dan memperbaiki bug dalam 15 menit dari stack trace + nama fungsi.

**P1 — Review plan** (sebelum koding)
Review rencana item terkait: scalable, rigid (kontrak jelas, sulit dipakai salah),
mudah dirawat/diperbaiki/ditambah fitur, mudah dinalar. Cari kelas bug yang bisa
dihilangkan lewat desain. Perbaiki langsung di `IMPLEMENTATION.md`/`DESIGN.md`
(in-place), jangan bikin dokumen baru.

**P2 — Simplify kode**
KISS, DRY, SRP, buang bloat. Buang state yang tidak perlu (variabel, field,
argumen, flag). Lindungi dari state explosion: kurangi kombinasi state yang
mungkin; turunkan nilai daripada menyimpannya; satu enum daripada beberapa bool.
Steelman tiap perubahan.

**P3 — Simplify test**
Gabungkan test yang menguji hal sama (parametrize), hapus test yang tidak bisa
gagal atau cuma mengulang implementasi. Test menguji perilaku lewat API publik,
bukan detail internal, supaya refactor tidak memecahkannya. Dilarang `sleep`
dan asumsi timing; pakai input deterministik. Steelman tiap penghapusan.

**P4 — Komentar**
Hapus komentar trivial (yang mengulang kode). Komentar yang tersisa menjelaskan
**kenapa** untuk hal yang tidak jelas: perilaku aneh library, keputusan desain,
jebakan. Tiap paragraf komentar harus punya alasan ada. Gaya: membumi, hangat,
ringkas, kalimat sederhana yang mudah dibaca non-native English, boleh bercerita
singkat ("calamine returns ints as floats, so ..."). Tanpa em-dash (—), tanpa
gaya khas tulisan AI. Bahasa komentar & docstring kode: Inggris.

**P5 — Review robustness**
Review perubahan terakhir dengan kriteria P1 (scalable, rigid, mudah dirawat,
2 pagi setahun lagi, single source of truth, hapus kelas bug). Perbaiki semua
temuan langsung, lalu jalankan §6.

**P6 — Verifikasi paralel** (sebelum menyatakan fase selesai, atau bila diminta)
Jalankan 3 subagent paralel dengan gaya prompt yang benar-benar berbeda:
1. **Kreatif / adversarial**: "coba rusak ini": input Excel aneh (sheet kosong,
   merged menyilang, header unicode, 1 juta baris, formula error, file palsu),
   urutan panggilan tool yang tidak lazim, agent yang salah pakai tool.
2. **Teliti / auditor**: baca diff baris per baris terhadap AGENTS.md §2–§5 dan
   kontrak di DESIGN.md: pelanggaran batas keras, schema tool tidak datar, error
   jadi exception, ID tidak deterministik, budget token, regresi.
3. **Pengguna nyata (agent LLM + host)**: berperan sebagai agent yang hanya
   melihat docstring & output tool, kerjakan tugas nyata pada file sampel
   (pahami workbook, usulkan dataset). Laporkan di mana ia bingung, salah tafsir,
   atau butuh terlalu banyak panggilan/token.
Tiap subagent kembalikan temuan ringkas dengan `file:line` + tingkat keparahan.
Gabungkan, buang duplikat, perbaiki yang valid, laporkan sisanya ke user.

**P7 — Refactor spaghetti** (bila user menunjuk modul X)
Rombak X dengan kriteria P1 + P5. Rencana dulu (ditulis singkat, steelman),
pertahankan perilaku publik, test lama harus tetap hijau sebelum dan sesudah.

**P8 — Commit** (hanya bila user minta)
Ikuti gaya `git log` yang ada. Pesan 2–5 kalimat, sedikit kata, membumi, mudah
dibaca non-native, bercerita singkat (apa masalahnya, apa yang diubah, hal
yang tidak jelas). Tanpa em-dash, tanpa gaya tulisan AI, tanpa daftar bullet
panjang. Tidak push kecuali diminta.
