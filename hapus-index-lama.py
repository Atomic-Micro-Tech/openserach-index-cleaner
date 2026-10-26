#!/usr/bin/env python3
"""Hapus index OpenSearch lama melalui HTTP API setiap hari pukul 01:00.

Contoh pemakaian:
    # Jalankan scheduler internal (default, menunggu sampai 01:00)
    OPENSEARCH_URL=https://localhost:9200 \
    OPENSEARCH_USER=admin OPENSEARCH_PASSWORD=rahasia \
    INDEX_PATTERN='logstash-*' RETENTION_DAYS=30 \
    python3 hapus-index-lama.py

    # Uji tanpa menghapus apa pun
    python3 hapus-index-lama.py --once --dry-run

    # Mode sekali jalan, cocok dipanggil dari cron pukul 01:00
    python3 hapus-index-lama.py --once

    # Cron (alternatif jika tidak ingin proses selalu berjalan)
    0 1 * * * /usr/bin/python3 /path/hapus-index-lama.py --once >> /var/log/hapus-index.log 2>&1

Konfigurasi dapat diberikan lewat argumen CLI atau environment variable.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import logging
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

LOGGER = logging.getLogger("hapus-index-lama")
MS_PER_DAY = 86_400_000
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ConfigError(Exception):
    """Konfigurasi tidak valid."""


class OpenSearchError(Exception):
    """Permintaan ke OpenSearch gagal."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Settings:
    base_url: str
    patterns: Tuple[str, ...]
    exclude_patterns: Tuple[str, ...]
    retention_days: int
    include_hidden: bool
    dry_run: bool
    max_delete: int
    once: bool
    schedule_hour: int
    schedule_minute: int
    tz: Optional[tzinfo]
    tz_name: str
    username: Optional[str]
    password: Optional[str]
    api_key: Optional[str]
    verify_tls: bool
    ca_cert: Optional[str]
    timeout: float
    retries: int
    verbose: bool
    log_file: Optional[str]


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Environment {name} harus bernilai true/false, bukan {raw!r}")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"Environment {name} harus berupa angka, bukan {raw!r}") from None


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"Environment {name} harus berupa angka, bukan {raw!r}") from None


def parse_schedule(value: str) -> Tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("format jam harus HH:MM, contoh 01:00")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError("format jam harus HH:MM, contoh 01:00") from None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise argparse.ArgumentTypeError("jam harus 00:00 sampai 23:59")
    return hour, minute


def parse_patterns(value: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    patterns = tuple(p.strip() for p in value.split(",") if p.strip())
    return patterns or default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hapus index OpenSearch yang lebih tua dari batas retensi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Environment variable:
  OPENSEARCH_URL          URL OpenSearch (default: http://localhost:9200)
  OPENSEARCH_USER         Username basic auth
  OPENSEARCH_PASSWORD     Password basic auth
  OPENSEARCH_API_KEY      API key (alternatif basic auth)
  OPENSEARCH_VERIFY_TLS   Verifikasi sertifikat TLS (default: true)
  OPENSEARCH_CA_CERT      Path CA certificate untuk TLS
  INDEX_PATTERN           Pola index, pisahkan koma (default: *)
  EXCLUDE_PATTERN         Pola index yang dilindungi, pisahkan koma
  RETENTION_DAYS          Hapus index lebih tua dari N hari (default: 30)
  INCLUDE_HIDDEN          Ikutkan index tersembunyi / berawalan titik (default: false)
  DRY_RUN                 Hanya tampilkan rencana (default: false)
  MAX_DELETE_PER_RUN      Batas index per run, 0 = tanpa batas (default: 0)
  SCHEDULE_TIME           Jam harian HH:MM (default: 01:00)
  TIMEZONE                Zona waktu jadwal, contoh Asia/Jakarta (default: lokal)
  HTTP_TIMEOUT            Timeout HTTP dalam detik (default: 30)
  HTTP_RETRIES            Jumlah retry untuk error sementara (default: 3)
  LOG_FILE                Simpan log ke file

Contoh:
  python3 hapus-index-lama.py --once --dry-run
  python3 hapus-index-lama.py --pattern 'logstash-*,filebeat-*' --retention-days 14
  python3 hapus-index-lama.py --time 01:00 --timezone Asia/Jakarta
""",
    )
    parser.add_argument("--url", default=os.environ.get("OPENSEARCH_URL", "http://localhost:9200"),
                        help="URL OpenSearch")
    parser.add_argument("--pattern", default=os.environ.get("INDEX_PATTERN", "*"),
                        help="Pola index yang diproses, pisahkan koma")
    parser.add_argument("--exclude", default=os.environ.get("EXCLUDE_PATTERN", ""),
                        help="Pola index yang tidak boleh dihapus, pisahkan koma")
    parser.add_argument("--retention-days", type=int,
                        default=env_int("RETENTION_DAYS", 30),
                        help="Hapus index yang lebih tua dari N hari")
    parser.add_argument("--time", dest="schedule_time", type=parse_schedule,
                        default=parse_schedule(os.environ.get("SCHEDULE_TIME", "01:00")),
                        help="Jam harian HH:MM (default: 01:00)")
    parser.add_argument("--timezone", default=os.environ.get("TIMEZONE", ""),
                        help="Zona waktu jadwal, contoh Asia/Jakarta")
    parser.add_argument("--username", default=os.environ.get("OPENSEARCH_USER", ""),
                        help="Username basic auth")
    parser.add_argument("--password", default=os.environ.get("OPENSEARCH_PASSWORD", ""),
                        help="Password basic auth (disarankan lewat environment variable)")
    parser.add_argument("--api-key", default=os.environ.get("OPENSEARCH_API_KEY", ""),
                        help="API key OpenSearch")
    parser.add_argument("--ca-cert", default=os.environ.get("OPENSEARCH_CA_CERT", ""),
                        help="Path CA certificate")
    parser.add_argument("--timeout", type=float,
                        default=env_float("HTTP_TIMEOUT", 30.0),
                        help="Timeout HTTP dalam detik")
    parser.add_argument("--retries", type=int,
                        default=env_int("HTTP_RETRIES", 3),
                        help="Jumlah retry untuk error HTTP sementara")
    parser.add_argument("--once", action="store_true",
                        help="Jalankan sekali lalu keluar (untuk cron)")
    parser.add_argument("--dry-run", action="store_true", default=env_bool("DRY_RUN", False),
                        help="Tampilkan index yang akan dihapus tanpa menghapus")
    parser.add_argument("--include-hidden", action="store_true",
                        default=env_bool("INCLUDE_HIDDEN", False),
                        help="Ikutkan index tersembunyi (berawalan titik)")
    parser.add_argument("--max-delete", type=int,
                        default=env_int("MAX_DELETE_PER_RUN", 0),
                        help="Batas jumlah index per run, 0 = tanpa batas")
    parser.add_argument("--log-file", default=os.environ.get("LOG_FILE", ""),
                        help="Simpan log ke file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log debug")
    parser.set_defaults(verify_tls=env_bool("OPENSEARCH_VERIFY_TLS", True))
    tls_group = parser.add_mutually_exclusive_group()
    tls_group.add_argument("--verify-tls", dest="verify_tls", action="store_true",
                           help="Verifikasi sertifikat TLS")
    tls_group.add_argument("--insecure", dest="verify_tls", action="store_false",
                           help="Jangan verifikasi sertifikat TLS")
    return parser


def build_settings(argv: Optional[Sequence[str]] = None) -> Settings:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.retention_days < 1:
        parser.error("--retention-days minimal 1")
    if args.max_delete < 0:
        parser.error("--max-delete tidak boleh negatif")
    if args.timeout <= 0:
        parser.error("--timeout harus lebih dari 0")
    if args.retries < 0:
        parser.error("--retries tidak boleh negatif")

    base_url = args.url.strip().rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        parser.error("--url harus berupa URL http:// atau https:// yang valid")
    if parsed.username or parsed.password:
        parser.error("jangan menaruh kredensial di URL; gunakan --username/--password")
    if parsed.query or parsed.fragment:
        parser.error("--url tidak boleh mengandung query atau fragment")

    username = args.username.strip() or None
    password = args.password if username else None
    api_key = args.api_key.strip() or None
    if api_key and (username or password):
        parser.error("gunakan --api-key atau --username/--password, jangan keduanya")
    if username and not password:
        parser.error("--password wajib diisi jika --username digunakan")

    ca_cert = args.ca_cert.strip() or None
    if ca_cert and not os.path.isfile(ca_cert):
        parser.error(f"CA certificate tidak ditemukan: {ca_cert}")

    log_file = args.log_file.strip() or None
    if log_file:
        parent = os.path.dirname(os.path.abspath(log_file))
        if not os.path.isdir(parent):
            parser.error(f"direktori log tidak ditemukan: {parent}")

    tz = None
    tz_name = "waktu lokal server"
    if args.timezone.strip():
        if ZoneInfo is None:
            parser.error("zona waktu memerlukan Python 3.9+ (modul zoneinfo)")
        try:
            tz = ZoneInfo(args.timezone.strip())
        except ZoneInfoNotFoundError:
            parser.error(f"zona waktu tidak dikenal: {args.timezone}")
        tz_name = args.timezone.strip()

    hour, minute = args.schedule_time
    return Settings(
        base_url=base_url,
        patterns=parse_patterns(args.pattern, ("*",)),
        exclude_patterns=parse_patterns(args.exclude, ()),
        retention_days=args.retention_days,
        include_hidden=args.include_hidden,
        dry_run=args.dry_run,
        max_delete=args.max_delete,
        once=args.once,
        schedule_hour=hour,
        schedule_minute=minute,
        tz=tz,
        tz_name=tz_name,
        username=username,
        password=password,
        api_key=api_key,
        verify_tls=args.verify_tls,
        ca_cert=ca_cert,
        timeout=args.timeout,
        retries=args.retries,
        verbose=args.verbose,
        log_file=log_file,
    )


class OpenSearchClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.base_url
        self.timeout = settings.timeout
        self.retries = settings.retries
        self.ssl_context = self._build_ssl_context(settings)

        headers = {
            "Accept": "application/json",
            "User-Agent": "hapus-index-lama/1.0",
        }
        if settings.api_key:
            key = settings.api_key
            if not key.lower().startswith("apikey "):
                key = f"ApiKey {key}"
            headers["Authorization"] = key
        elif settings.username:
            token = base64.b64encode(
                f"{settings.username}:{settings.password or ''}".encode("utf-8")
            ).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
            if settings.base_url.startswith("http://"):
                LOGGER.warning("Kredensial dikirim melalui HTTP tanpa TLS; gunakan HTTPS")
        self.headers = headers

    @staticmethod
    def _build_ssl_context(settings: Settings) -> Optional[ssl.SSLContext]:
        if not settings.base_url.startswith("https://"):
            return None
        context = ssl.create_default_context(cafile=settings.ca_cert)
        if not settings.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            LOGGER.warning("Verifikasi sertifikat TLS dimatikan")
        return context

    def request(self, method: str, path: str) -> Optional[object]:
        url = f"{self.base_url}{path}"
        last_error: Optional[OpenSearchError] = None

        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, headers=self.headers, method=method)
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout, context=self.ssl_context
                ) as response:
                    return self._decode(response.read(), response.status)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                last_error = OpenSearchError(
                    f"HTTP {exc.code} pada {method} {path}: {body[:500]}",
                    status=exc.code,
                    body=body,
                )
                if exc.code not in RETRYABLE_STATUS or attempt >= self.retries:
                    raise last_error from None
                LOGGER.warning("HTTP %s pada %s %s; mencoba lagi (%d/%d)",
                               exc.code, method, path, attempt + 1, self.retries)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = OpenSearchError(
                    f"Gagal terhubung ke OpenSearch ({method} {path}): {exc}"
                )
                if attempt >= self.retries:
                    raise last_error from None
                LOGGER.warning("Koneksi gagal (%s); mencoba lagi (%d/%d)",
                               exc, attempt + 1, self.retries)
            time.sleep(min(2 ** attempt, 10))

        assert last_error is not None
        raise last_error

    @staticmethod
    def _decode(raw: bytes, status: int) -> Optional[object]:
        if not raw:
            return None
        text = raw.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise OpenSearchError(
                f"Respons OpenSearch bukan JSON (HTTP {status}): {text[:300]}",
                status=status,
                body=text,
            ) from None

    def list_indices(self) -> List[Dict[str, object]]:
        query = urllib.parse.urlencode({
            "format": "json",
            "h": "index,creation.date,creation.date.string",
            "expand_wildcards": "all",
            "s": "index",
        })
        result = self.request("GET", f"/_cat/indices?{query}")
        if not isinstance(result, list):
            raise OpenSearchError(f"Respons _cat/indices tidak sesuai: {result!r}")
        return [item for item in result if isinstance(item, dict)]

    def delete_index(self, name: str) -> Optional[object]:
        encoded = urllib.parse.quote(name, safe="")
        return self.request("DELETE", f"/{encoded}")


def parse_creation_ms(item: Dict[str, object]) -> Optional[int]:
    raw = item.get("creation.date")
    if raw not in (None, ""):
        try:
            value = int(str(raw))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass

    raw = item.get("creation.date.string")
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        created = datetime.fromisoformat(text)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return int(created.timestamp() * 1000)


def is_protected(name: str, settings: Settings, skipped: Dict[str, int]) -> bool:
    if name.startswith(".ds-"):
        skipped["data_stream"] += 1
        LOGGER.debug("Lewati backing index data stream: %s", name)
        return True
    if name.startswith(".") and not settings.include_hidden:
        skipped["hidden"] += 1
        LOGGER.debug("Lewati index tersembunyi: %s", name)
        return True
    if not any(fnmatch.fnmatchcase(name, pattern) for pattern in settings.patterns):
        skipped["pattern"] += 1
        LOGGER.debug("Lewati index di luar pola: %s", name)
        return True
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in settings.exclude_patterns):
        skipped["excluded"] += 1
        LOGGER.debug("Lewati index yang dikecualikan: %s", name)
        return True
    return False


def select_old_indices(
    settings: Settings,
    items: Sequence[Dict[str, object]],
    now_ms: int,
) -> List[Tuple[str, int]]:
    skipped = {
        "data_stream": 0,
        "hidden": 0,
        "pattern": 0,
        "excluded": 0,
        "no_creation": 0,
    }
    selected: List[Tuple[str, int]] = []

    for item in items:
        name = item.get("index")
        if not isinstance(name, str) or not name:
            continue
        if is_protected(name, settings, skipped):
            continue
        created_ms = parse_creation_ms(item)
        if created_ms is None:
            skipped["no_creation"] += 1
            LOGGER.warning("Lewati %s: tanggal pembuatan index tidak diketahui", name)
            continue
        age_ms = now_ms - created_ms
        if age_ms >= settings.retention_days * MS_PER_DAY:
            selected.append((name, created_ms))

    selected.sort(key=lambda entry: entry[1])
    LOGGER.info(
        "Total index: %d | memenuhi syarat: %d | dilewati: tersembunyi=%d, pola=%d, "
        "dikecualikan=%d, data_stream=%d, tanpa tanggal=%d",
        len(items), len(selected), skipped["hidden"], skipped["pattern"],
        skipped["excluded"], skipped["data_stream"], skipped["no_creation"],
    )
    return selected


def describe_index(name: str, created_ms: int, now_ms: int) -> str:
    created = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
    age_days = (now_ms - created_ms) / MS_PER_DAY
    return f"{name} (dibuat {created:%Y-%m-%d %H:%M:%S} UTC, umur {age_days:.1f} hari)"


def run_cleanup(settings: Settings, client: OpenSearchClient) -> int:
    started = time.monotonic()
    LOGGER.info("Memeriksa index OpenSearch di %s", settings.base_url)
    items = client.list_indices()
    now_ms = int(time.time() * 1000)
    selected = select_old_indices(settings, items, now_ms)

    if settings.max_delete and len(selected) > settings.max_delete:
        LOGGER.warning(
            "Ditemukan %d index memenuhi syarat; dibatasi %d per run",
            len(selected), settings.max_delete,
        )
        selected = selected[:settings.max_delete]

    if not selected:
        LOGGER.info("Tidak ada index yang perlu dihapus (retensi %d hari)",
                    settings.retention_days)
        return 0

    if settings.dry_run:
        LOGGER.info("DRY RUN: %d index akan dihapus:", len(selected))
        for name, created_ms in selected:
            LOGGER.info("  - %s", describe_index(name, created_ms, now_ms))
        LOGGER.info("DRY RUN selesai dalam %.1f detik; tidak ada index yang dihapus",
                    time.monotonic() - started)
        return 0

    deleted = 0
    missing = 0
    failed: List[str] = []
    for name, created_ms in selected:
        LOGGER.info("Menghapus %s", describe_index(name, created_ms, now_ms))
        try:
            result = client.delete_index(name)
        except OpenSearchError as exc:
            if exc.status == 404:
                missing += 1
                LOGGER.warning("Index sudah tidak ada: %s", name)
            else:
                failed.append(name)
                LOGGER.error("Gagal menghapus %s: %s", name, exc)
            continue
        if isinstance(result, dict) and result.get("acknowledged") is False:
            failed.append(name)
            LOGGER.error("OpenSearch tidak mengakui penghapusan %s: %s", name, result)
        else:
            deleted += 1
            LOGGER.info("Berhasil dihapus: %s", name)

    LOGGER.info(
        "Selesai dalam %.1f detik: dihapus=%d, sudah tidak ada=%d, gagal=%d",
        time.monotonic() - started, deleted, missing, len(failed),
    )
    if failed:
        LOGGER.error("Index yang gagal dihapus: %s", ", ".join(failed))
        return 1
    return 0


def now_in(tz: Optional[tzinfo]) -> datetime:
    return datetime.now(tz) if tz else datetime.now()


def run_scheduler(settings: Settings, client: OpenSearchClient) -> int:
    LOGGER.info(
        "Scheduler aktif: setiap hari pukul %02d:%02d (%s). Tekan Ctrl+C untuk berhenti.",
        settings.schedule_hour, settings.schedule_minute, settings.tz_name,
    )
    while True:
        now = now_in(settings.tz)
        target = now.replace(
            hour=settings.schedule_hour,
            minute=settings.schedule_minute,
            second=0,
            microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        LOGGER.info("Pembersihan berikutnya: %s",
                    target.strftime("%Y-%m-%d %H:%M:%S %Z").strip())

        while True:
            remaining = (target - now_in(settings.tz)).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 30.0))

        try:
            run_cleanup(settings, client)
        except Exception:
            LOGGER.exception("Pembersihan gagal; akan dicoba lagi pada jadwal berikutnya")


def setup_logging(verbose: bool, log_file: Optional[str]) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        settings = build_settings(argv)
    except (ConfigError, argparse.ArgumentTypeError) as exc:
        print(f"Error konfigurasi: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.verbose, settings.log_file)
    if "*" in settings.patterns:
        LOGGER.warning(
            "Pola '*' memproses semua index non-tersembunyi; "
            "gunakan --pattern untuk membatasi ke index log saja"
        )

    try:
        client = OpenSearchClient(settings)
        if settings.once:
            return run_cleanup(settings, client)
        return run_scheduler(settings, client)
    except KeyboardInterrupt:
        LOGGER.info("Dihentikan oleh pengguna")
        return 130
    except (OpenSearchError, OSError) as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
