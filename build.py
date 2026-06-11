#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генератор доменных списков для Keenetic DNS-Based Routes из v2fly/domain-list-community.

Выходной формат каждого output/*.txt:
    domain.tld
    sub.domain.tld

Без комментариев, без wildcard (*), без DOMAIN-SUFFIX, без domain:, full:, keyword:, regexp:.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

DLC_ZIP_URL = "https://github.com/v2fly/domain-list-community/archive/refs/heads/master.zip"
PROJECT_ROOT = Path(__file__).resolve().parent
SERVICES_FILE = PROJECT_ROOT / "services.txt"
CACHE_DIR = PROJECT_ROOT / "_cache"
SOURCE_DIR = CACHE_DIR / "domain-list-community-master"
DATA_DIR = SOURCE_DIR / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
SKIPPED_DIR = OUTPUT_DIR / "_skipped"

DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$", re.IGNORECASE)


def log(msg: str) -> None:
    print(msg, flush=True)


def download_and_extract(force: bool = True) -> None:
    """Скачивает свежий архив DLC и распаковывает его в _cache."""
    if force and CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if DATA_DIR.exists() and not force:
        log("[source] Использую уже скачанный domain-list-community")
        return

    log("[source] Скачиваю свежий domain-list-community...")
    with TemporaryDirectory() as td:
        zip_path = Path(td) / "dlc.zip"
        urllib.request.urlretrieve(DLC_ZIP_URL, zip_path)

        log("[source] Распаковываю архив...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(CACHE_DIR)

    if not DATA_DIR.exists():
        raise RuntimeError(f"Не найдена папка data: {DATA_DIR}")


def parse_services(path: Path) -> dict[str, list[str]]:
    services: dict[str, list[str]] = {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"services.txt:{line_no}: нет '=' в строке: {raw}")
        out_name, sources_raw = line.split("=", 1)
        out_name = out_name.strip().lower()
        sources = [x.strip().lower() for x in sources_raw.split(",") if x.strip()]
        if not out_name or not sources:
            raise ValueError(f"services.txt:{line_no}: пустое имя или список источников: {raw}")
        services[out_name] = sources
    return services


def remove_inline_comment_and_attrs(raw: str) -> str:
    """Убирает комментарии и атрибуты DLC вида @ads, @cn."""
    line = raw.strip()
    if not line or line.startswith("#"):
        return ""
    # В DLC комментарий начинается с # после пробела или в начале строки.
    # URL с # нам здесь не нужен, поэтому простое отсечение безопасно.
    if "#" in line:
        line = line.split("#", 1)[0].strip()
    if not line:
        return ""
    # Убираем атрибуты после пробела: example.com @ads @cn
    parts = line.split()
    return parts[0].strip()


def normalize_domain(value: str) -> tuple[str | None, str | None]:
    """Возвращает (domain, reason). Если domain=None, reason объясняет пропуск."""
    v = value.strip().lower()

    if not v:
        return None, "empty"

    if v.startswith(("keyword:", "regexp:")):
        return None, "unsupported_keyword_or_regexp"

    if v.startswith("include:"):
        return None, "include_not_domain"

    if v.startswith("domain:"):
        v = v[len("domain:"):]
    elif v.startswith("full:"):
        v = v[len("full:"):]

    v = v.strip().strip(".")

    # Keenetic не требует * — поддомены указанного имени включаются автоматически.
    if v.startswith("*."):
        v = v[2:]

    if v.startswith(("http://", "https://")):
        return None, "url_not_domain"

    if "/" in v or ":" in v:
        return None, "not_plain_domain"

    try:
        ipaddress.ip_address(v)
        return None, "ip_address_skipped"
    except ValueError:
        pass

    # IDN -> punycode, если нужно.
    try:
        v = v.encode("idna").decode("ascii")
    except UnicodeError:
        return None, "bad_idn"

    # Для Keenetic-списка оставляем обычные FQDN с точкой.
    # Строки без точки вроде "youtube" из DLC не добавляем.
    if "." not in v:
        return None, "no_dot_domain"

    if not DOMAIN_RE.match(v):
        return None, "bad_domain_syntax"

    return v, None


def read_dlc_list(list_name: str, seen: set[str] | None = None) -> tuple[set[str], list[str]]:
    """Читает data/<list_name>, раскрывает include:, возвращает домены и skipped-строки."""
    if seen is None:
        seen = set()

    list_name = list_name.strip().lower()
    list_name = list_name.split("@", 1)[0]  # базовая поддержка include:name@attr

    if list_name in seen:
        return set(), [f"cycle include:{list_name}"]
    seen.add(list_name)

    path = DATA_DIR / list_name
    if not path.exists():
        return set(), [f"missing list: {list_name}"]

    domains: set[str] = set()
    skipped: list[str] = []

    for line_no, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        cleaned = remove_inline_comment_and_attrs(raw)
        if not cleaned:
            continue

        if cleaned.startswith("include:"):
            include_name = cleaned[len("include:"):].strip().lower()
            child_domains, child_skipped = read_dlc_list(include_name, seen)
            domains.update(child_domains)
            skipped.extend(child_skipped)
            continue

        domain, reason = normalize_domain(cleaned)
        if domain:
            domains.add(domain)
        else:
            skipped.append(f"{list_name}:{line_no}: {reason}: {raw.strip()}")

    return domains, skipped


def write_outputs(services: dict[str, list[str]]) -> None:
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SKIPPED_DIR.mkdir(parents=True, exist_ok=True)

    for out_name, sources in services.items():
        all_domains: set[str] = set()
        all_skipped: list[str] = []

        log(f"[build] {out_name}.txt <= {', '.join(sources)}")
        for src in sources:
            domains, skipped = read_dlc_list(src, seen=set())
            all_domains.update(domains)
            all_skipped.extend(skipped)

        out_path = OUTPUT_DIR / f"{out_name}.txt"
        out_path.write_text("\n".join(sorted(all_domains)) + ("\n" if all_domains else ""), encoding="utf-8", newline="\n")

        skipped_path = SKIPPED_DIR / f"{out_name}-skipped.txt"
        skipped_path.write_text("\n".join(all_skipped) + ("\n" if all_skipped else ""), encoding="utf-8", newline="\n")

        log(f"[ok] {out_path.relative_to(PROJECT_ROOT)}: {len(all_domains)} доменов, skipped: {len(all_skipped)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Keenetic domain lists from v2fly/domain-list-community")
    parser.add_argument("--no-download", action="store_true", help="не скачивать заново, использовать _cache")
    args = parser.parse_args()

    if not SERVICES_FILE.exists():
        print(f"Не найден {SERVICES_FILE}", file=sys.stderr)
        return 1

    try:
        download_and_extract(force=not args.no_download)
        services = parse_services(SERVICES_FILE)
        write_outputs(services)
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1

    log("Готово. Файлы находятся в папке output/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
