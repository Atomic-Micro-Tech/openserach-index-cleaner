# Hapus Index OpenSearch Otomatis

Script Python untuk menghapus index OpenSearch yang lebih tua dari batas retensi melalui HTTP API, dijalankan otomatis setiap hari pukul **01:00** menggunakan **systemd timer** di Ubuntu/Debian.

## Daftar Isi

- [Ringkasan](#ringkasan)
- [Prasyarat](#prasyarat)
- [Struktur File](#struktur-file)
- [Langkah 1 — Siapkan Script](#langkah-1--siapkan-script)
- [Langkah 2 — Buat File Environment](#langkah-2--buat-file-environment)
- [Menggunakan File .env (Opsional)](#menggunakan-file-env-opsional)
- [Langkah 3 — Buat systemd Service](#langkah-3--buat-systemd-service)
- [Langkah 4 — Buat systemd Timer](#langkah-4--buat-systemd-timer)
- [Langkah 5 — Aktifkan Timer](#langkah-5--aktifkan-timer)
- [Langkah 6 — Uji Manual Sebelum Produksi](#langkah-6--uji-manual-sebelum-produksi)
- [Langkah 7 — Verifikasi](#langkah-7--verifikasi)
- [Referensi Opsi Script](#referensi-opsi-script)
- [Log dan Monitoring](#log-dan-monitoring)
- [Troubleshooting](#troubleshooting)
- [Mengganti Jadwal](#mengganti-jadwal)
- [Mematikan dan Menghapus](#mematikan-dan-menghapus)

## Ringkasan

| Item | Nilai |
| --- | --- |
| Script | `hapus-index-lama.py` |
| Scheduler | systemd timer `hapus-index.timer` |
| Jadwal default | Setiap hari pukul `01:00:00` |
| Mode eksekusi | `--once` (sekali jalan lalu keluar) |
| Dependency | Python 3.9+ standard library (tanpa `pip install`) |
| Konfigurasi | `/etc/hapus-index/hapus-index.env` |

Keuntungan systemd timer dibanding cron:

- `Persistent=true` — job tetap dijalankan bila server sempat mati saat jadwal.
- Log terpusat di journald (`journalctl`), tanpa perlu mengatur redirect.
- Status dan jadwal berikutnya bisa diperiksa lewat `systemctl list-timers`.
- Timezone mengikuti timezone sistem dan mudah diganti tanpa mengedit crontab.

## Prasyarat

- Ubuntu 20.04 / 22.04 / 24.04 atau Debian 11+ dengan systemd.
- Python 3.9 atau lebih baru (dibutuhkan modul `zoneinfo`).
- Akses jaringan ke endpoint OpenSearch.
- User yang menjalankan service harus bisa membaca script dan file environment.

Periksa versi Python dan systemd:

```bash
python3 --version
systemctl --version | head -1
```

## Struktur File

```text
/opt/hapus-index/hapus-index-lama.py        # script utama
/etc/hapus-index/hapus-index.env            # konfigurasi (kredensial, retensi)
/etc/systemd/system/hapus-index.service     # unit service
/etc/systemd/system/hapus-index.timer       # unit timer
```

## Langkah 1 — Siapkan Script dan User

Buat user khusus tanpa login agar service tidak berjalan sebagai `root`:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin opensearch-cleanup
```

Jika user sudah ada, `useradd` akan menampilkan peringatan dan bisa diabaikan. Periksa dengan `id opensearch-cleanup`.

Salin script dan README ke direktori aplikasi, lalu pastikan dapat dieksekusi:

```bash
sudo mkdir -p /opt/hapus-index
sudo cp hapus-index-lama.py README.md /opt/hapus-index/
sudo chmod 755 /opt/hapus-index/hapus-index-lama.py
sudo chown -R opensearch-cleanup:opensearch-cleanup /opt/hapus-index

/usr/bin/python3 /opt/hapus-index/hapus-index-lama.py --help
```

Jika script Anda berada di direktori lain, cukup sesuaikan path pada `ExecStart` di Langkah 3.

## Langkah 2 — Buat File Environment

Kredensial OpenSearch tidak ditulis di unit file agar tidak terlihat oleh semua user melalui `systemctl cat`.

```bash
sudo mkdir -p /etc/hapus-index
sudo nano /etc/hapus-index/hapus-index.env
```

Isi file:

```ini
OPENSEARCH_URL=https://localhost:9200
OPENSEARCH_USER=admin
OPENSEARCH_PASSWORD=ganti-dengan-password-anda

# Alternatif autentikasi: gunakan API key dan hapus dua baris user/password di atas
# OPENSEARCH_API_KEY=base64-id:base64-api-key

# Pola index yang diproses (wajib disesuaikan dengan nama index Anda)
INDEX_PATTERN=logstash-*,filebeat-*

# Index yang tidak boleh dihapus
EXCLUDE_PATTERN=logstash-important-*

# Retensi dalam hari
RETENTION_DAYS=30

# Opsional
OPENSEARCH_VERIFY_TLS=true
# OPENSEARCH_CA_CERT=/etc/hapus-index/ca.crt
HTTP_TIMEOUT=30
HTTP_RETRIES=3
MAX_DELETE_PER_RUN=0
DRY_RUN=false
LOG_FILE=/var/log/hapus-index/hapus-index.log
```

Batasi izin file karena berisi password. Pemilik tetap `root`, grup `opensearch-cleanup` agar user service bisa membacanya untuk uji manual:

```bash
sudo chown root:opensearch-cleanup /etc/hapus-index/hapus-index.env
sudo chmod 640 /etc/hapus-index/hapus-index.env
```

> **Penting:** pola default `*` akan memproses **semua** index non-tersembunyi. Selalu batasi `INDEX_PATTERN` ke index log Anda agar index sistem tidak terhapus.

Siapkan direktori log bila `LOG_FILE` digunakan:

```bash
sudo mkdir -p /var/log/hapus-index
sudo chown opensearch-cleanup:opensearch-cleanup /var/log/hapus-index
sudo chmod 750 /var/log/hapus-index
```

## Menggunakan File .env (Opsional)

Script dapat memuat konfigurasi sendiri dari file `.env`, tanpa bergantung pada `EnvironmentFile=` systemd. Ini berguna untuk eksekusi manual, cron, Docker, atau pengujian lokal.

```bash
# Muat file tertentu
python3 hapus-index-lama.py --env-file /etc/hapus-index/hapus-index.env --once --dry-run

# Tanpa --env-file, script otomatis memuat .env di direktori kerja bila ada
python3 hapus-index-lama.py --once

# Abaikan file .env sepenuhnya
python3 hapus-index-lama.py --no-env-file --once
```

Nama file juga dapat diberikan lewat environment variable `ENV_FILE`:

```bash
ENV_FILE=/etc/hapus-index/hapus-index.env python3 hapus-index-lama.py --once
```

Contoh isi file:

```ini
# komentar di awal baris diabaikan
OPENSEARCH_URL=https://localhost:9200
export RETENTION_DAYS=30
INDEX_PATTERN="logstash-*,filebeat-*"   # komentar setelah nilai
EXCLUDE_PATTERN='logstash-important-*'
OPENSEARCH_PASSWORD="p@ss word#1"
```

Aturan parsing:

- Format `KEY=VALUE`; awalan `export ` diperbolehkan.
- Nilai boleh memakai kutip ganda (mendukung escape `\n`, `\t`, `\r`, `\"`, `\\`, `\$`) atau kutip tunggal (literal).
- Komentar `#` di awal baris selalu diabaikan. Komentar di akhir baris hanya berlaku bila didahului spasi dan nilai tidak dikutip.
- Variabel yang sudah ada di environment **tidak** ditimpa oleh isi file.
- Urutan prioritas nilai: argumen CLI → environment variable → isi `.env` → nilai default.
- `--no-env-file` membatalkan pemuatan `.env`, termasuk yang ditunjuk `ENV_FILE`.
- File yang ditunjuk `--env-file`/`ENV_FILE` wajib ada; bila tidak, script berhenti dengan exit code `2`.
- Path relatif dihitung dari direktori kerja saat script dijalankan, bukan dari lokasi script.

## Langkah 3 — Buat systemd Service

```bash
sudo nano /etc/systemd/system/hapus-index.service
```

```ini
[Unit]
Description=Hapus index OpenSearch lama
Documentation=file:///opt/hapus-index/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=opensearch-cleanup
Group=opensearch-cleanup
EnvironmentFile=/etc/hapus-index/hapus-index.env
ExecStart=/usr/bin/python3 /opt/hapus-index/hapus-index-lama.py --once
TimeoutStartSec=30min
Nice=10

# Sedikit pengerasan keamanan
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/log/hapus-index

[Install]
WantedBy=multi-user.target
```

Catatan:

- `Type=oneshot` karena script berjalan sekali lalu keluar.
- `--once` wajib dipakai di unit ini; tanpa itu script akan berjalan sebagai scheduler internal dan menggantung.
- `User=` dan `Group=` mengarah ke user yang dibuat di Langkah 1.
- Jika script terpaksa dijalankan sebagai `root` (misalnya CA cert hanya bisa dibaca root), ganti `User=` dan `Group=` menjadi `root`, dan sesuaikan `ReadWritePaths` serta pemilik direktori log.
- `TimeoutStartSec=30min` mencegah service dianggap menggantung saat menghapus ribuan index.

## Langkah 4 — Buat systemd Timer

```bash
sudo nano /etc/systemd/system/hapus-index.timer
```

```ini
[Unit]
Description=Jadwal harian hapus index OpenSearch pukul 01:00

[Timer]
# Setiap hari pukul 01:00 waktu lokal server
OnCalendar=*-*-* 01:00:00
# Jalankan segera bila jadwal terlewat saat server mati
Persistent=true
# Beri jeda acak agar tidak menumpuk dengan job lain
RandomizedDelaySec=300
Unit=hapus-index.service

[Install]
WantedBy=timers.target
```

Verifikasi ekspresi jadwal sebelum mengaktifkan:

```bash
systemd-analyze calendar '*-*-* 01:00:00'
```

> `RandomizedDelaySec=300` membuat eksekusi bisa mulai antara 01:00 dan 01:05. Hapus baris itu bila waktu eksekusi harus tepat 01:00.

Contoh lain:

```ini
OnCalendar=*-*-* 01:00:00        # setiap hari 01:00
OnCalendar=Mon..Fri 01:00:00    # hari kerja saja
OnCalendar=*-*-01 01:00:00      # tanggal 1 setiap bulan
```

## Langkah 5 — Aktifkan Timer

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hapus-index.timer
```

Pastikan timezone server sudah benar karena `OnCalendar` memakai waktu lokal:

```bash
timedatectl
sudo timedatectl set-timezone Asia/Jakarta
timedatectl
```

## Langkah 6 — Uji Manual Sebelum Produksi

Selalu uji dengan `DRY_RUN=true` lebih dulu agar tidak ada index yang terhapus.

1. Set `DRY_RUN=true` pada `/etc/hapus-index/hapus-index.env`.
2. Jalankan service sekali:

   ```bash
   sudo systemctl start hapus-index.service
   sudo journalctl -u hapus-index.service -n 50 --no-pager
   ```

3. Periksa daftar index yang dilaporkan akan dihapus. Jika sudah sesuai, set `DRY_RUN=false` lalu uji sekali lagi:

   ```bash
   sudo systemctl start hapus-index.service
   sudo journalctl -u hapus-index.service -n 50 --no-pager
   ```

Alternatif uji langsung tanpa systemd:

```bash
sudo -u opensearch-cleanup bash -c 'set -a; . /etc/hapus-index/hapus-index.env; set +a; \
  /usr/bin/python3 /opt/hapus-index/hapus-index-lama.py --once --dry-run'
```

## Langkah 7 — Verifikasi

```bash
# Timer aktif dan kapan eksekusi berikutnya
systemctl status hapus-index.timer --no-pager
systemctl list-timers hapus-index.timer --no-pager

# Status eksekusi terakhir
systemctl status hapus-index.service --no-pager

# Log lengkap
journalctl -u hapus-index.service -n 100 --no-pager
```

Contoh keluaran `list-timers` yang benar:

```text
NEXT                        LEFT     LAST                        PASSED   UNIT                ACTIVATES
Tue 2026-10-27 01:00:00 WIB 13h left Mon 2026-10-26 01:00:00 WIB 10h ago  hapus-index.timer   hapus-index.service
```

## Referensi Opsi Script

Semua opsi dapat diberikan lewat CLI, environment variable, atau file `.env` (lihat [Menggunakan File .env](#menggunakan-file-env-opsional)).

| Argumen | Environment | Default | Keterangan |
| --- | --- | --- | --- |
| `--env-file` | `ENV_FILE` | `.env` bila ada | Muat konfigurasi dari file `.env` |
| `--no-env-file` | — | mati | Jangan memuat file `.env` |
| `--url` | `OPENSEARCH_URL` | `http://localhost:9200` | Endpoint OpenSearch |
| `--pattern` | `INDEX_PATTERN` | `*` | Pola index diproses, pisahkan koma |
| `--exclude` | `EXCLUDE_PATTERN` | kosong | Pola index yang dilindungi |
| `--retention-days` | `RETENTION_DAYS` | `30` | Hapus index lebih tua dari N hari |
| `--time` | `SCHEDULE_TIME` | `01:00` | Hanya untuk scheduler internal |
| `--timezone` | `TIMEZONE` | lokal | Hanya untuk scheduler internal |
| `--username` | `OPENSEARCH_USER` | kosong | Basic auth |
| `--password` | `OPENSEARCH_PASSWORD` | kosong | Basic auth |
| `--api-key` | `OPENSEARCH_API_KEY` | kosong | Alternatif basic auth |
| `--ca-cert` | `OPENSEARCH_CA_CERT` | kosong | CA certificate untuk TLS |
| `--verify-tls` / `--insecure` | `OPENSEARCH_VERIFY_TLS` | `true` | Verifikasi sertifikat TLS |
| `--timeout` | `HTTP_TIMEOUT` | `30` | Timeout HTTP (detik) |
| `--retries` | `HTTP_RETRIES` | `3` | Retry untuk 429/5xx |
| `--once` | — | mati | Jalankan sekali lalu keluar |
| `--dry-run` | `DRY_RUN` | `false` | Tampilkan rencana tanpa menghapus |
| `--include-hidden` | `INCLUDE_HIDDEN` | `false` | Ikutkan index berawalan titik |
| `--max-delete` | `MAX_DELETE_PER_RUN` | `0` | Batas index per run, `0` = tanpa batas |
| `--log-file` | `LOG_FILE` | kosong | Simpan log ke file |
| `-v` | — | mati | Log debug |

Pengamanan bawaan:

- Backing index data stream `.ds-*` selalu dilewati.
- Index tersembunyi (berawalan `.`) dilewati kecuali `--include-hidden`.
- Index tanpa tanggal pembuatan tidak dihapus.
- Setiap penghapusan dicatat di log beserta umur index.

## Log dan Monitoring

Lihat log real-time:

```bash
journalctl -u hapus-index.service -f
```

Batasi periode:

```bash
journalctl -u hapus-index.service --since "24 hours ago" --no-pager
journalctl -u hapus-index.service --since today --no-pager | grep -E "Selesai|gagal"
```

Batas ukuran journal agar tidak memenuhi disk — edit `/etc/systemd/journald.conf`:

```ini
SystemMaxUse=500M
MaxRetentionSec=1month
```

```bash
sudo systemctl restart systemd-journald
```

Karena `Type=oneshot` dan timer terpisah, kegagalan tidak memicu restart otomatis. Pantau dengan:

```bash
systemctl is-failed hapus-index.service
```

Alternatif alert sederhana via `OnFailure=` pada `[Unit]` service:

```ini
OnFailure=hapus-index-alert.service
```

## Troubleshooting

### Service gagal dengan exit code 1

```bash
sudo systemctl status hapus-index.service --no-pager
sudo journalctl -u hapus-index.service -n 100 --no-pager
```

Penyebab umum: endpoint tidak dapat dijangkau, kredensial salah, atau TLS tidak valid.

### `ModuleNotFoundError: No module named 'zoneinfo'`

Python terlalu lama. Pasang Python 3.9+ dan sesuaikan `ExecStart`, misalnya `/usr/bin/python3.11`.

### Timer tidak berjalan

```bash
systemctl list-timers --all | grep hapus-index
sudo systemctl daemon-reload
sudo systemctl restart hapus-index.timer
```

Pastikan unit berada di `/etc/systemd/system/` dan sudah `enable` (bukan hanya `start`).

### Exit code 2 — konfigurasi tidak valid

Script menolak konfigurasi berbahaya seperti `RETENTION_DAYS=0`, `--time 25:00`, atau `--url` tanpa skema `http`/`https`. Pesan detail ada di journal.

### Kredensial ditolak / 401

- Periksa `OPENSEARCH_USER` dan `OPENSEARCH_PASSWORD`.
- Bila memakai API key, hapus baris user/password dari env karena keduanya tidak boleh dipakai bersamaan.
- Pastikan user di field `User=` pada unit bisa membaca env: `sudo -u opensearch-cleanup cat /etc/hapus-index/hapus-index.env`.

### Sertifikat TLS tidak dipercaya

Gunakan CA resmi:

```ini
OPENSEARCH_CA_CERT=/etc/hapus-index/ca.crt
OPENSEARCH_VERIFY_TLS=true
```

Hindari `OPENSEARCH_VERIFY_TLS=false` di produksi.

### Tidak ada index yang dihapus

Periksa pola dan retensi. Jalankan dengan `-v`:

```bash
sudo -u opensearch-cleanup bash -c 'set -a; . /etc/hapus-index/hapus-index.env; set +a; \
  /usr/bin/python3 /opt/hapus-index/hapus-index-lama.py --once --dry-run -v'
```

Log akan menampilkan jumlah index yang dilewati karena pola, tersembunyi, dikecualikan, data stream, atau tanpa tanggal.

### `Read-only file system` saat menulis log

`ProtectSystem=strict` membuat filesystem read-only kecuali pada `ReadWritePaths`. Tambahkan direktori tulis:

```ini
ReadWritePaths=/var/log/hapus-index
```

## Mengganti Jadwal

```bash
sudo systemctl edit --full hapus-index.timer
```

Ubah `OnCalendar`, lalu:

```bash
sudo systemctl daemon-reload
sudo systemctl restart hapus-index.timer
systemctl list-timers hapus-index.timer --no-pager
```

Mengubah file env tidak memerlukan restart timer, tetapi service harus dijalankan ulang untuk uji manual:

```bash
sudo systemctl start hapus-index.service
```

## Mematikan dan Menghapus

Matikan sementara:

```bash
sudo systemctl stop hapus-index.timer
sudo systemctl disable hapus-index.timer
```

Hapus permanen:

```bash
sudo systemctl disable --now hapus-index.timer
sudo rm /etc/systemd/system/hapus-index.timer
sudo rm /etc/systemd/system/hapus-index.service
sudo systemctl daemon-reload
sudo systemctl reset-failed
```

## Catatan

- Jangan menjalankan cron **dan** systemd timer untuk script yang sama — penghapusan dapat terjadi dua kali dalam satu hari.
- Untuk eksekusi lebih dari sekali sehari, gunakan beberapa baris `OnCalendar` pada timer:

  ```ini
  OnCalendar=*-*-* 01:00:00
  OnCalendar=*-*-* 13:00:00
  ```

- Uji selalu dengan `--dry-run` sebelum mengubah retensi atau pola index.
