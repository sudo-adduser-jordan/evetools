#!/usr/bin/env python3
"""Build estimated ISK costs for COSMOS shopping lists.

Data sources:
  * Fuzzwork market aggregates (Jita 4-4 / The Forge) for market prices.
  * EVE Ref public-contract snapshots for contract prices.
  * Fuzzwork SDE invTypes.csv for item name -> type ID resolution.

No ESI calls are made. Run this from the repository root or via GitHub Actions.
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import math
import re
import sys
import tarfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FUZZWORK_TYPES_URL = "https://www.fuzzwork.co.uk/dump/latest/csv/invTypes.csv"
FUZZWORK_AGGREGATES_URL = "https://market.fuzzwork.co.uk/aggregates/"
FUZZWORK_ORDERSET_URL = "https://market.fuzzwork.co.uk/api/orderset"
EVEREF_CONTRACTS_URL = "https://data.everef.net/public-contracts/public-contracts-latest.v2.tar.bz2"

JITA_STATION_ID = 60003760
THE_FORGE_REGION_ID = 10000002
USER_AGENT = "evetools-cosmos-costs/1.0 (+https://github.com/AyoKeito/eve-toolkit)"

# Fuzzwork's SDE names differ slightly from the names used in the COSMOS tables.
ITEM_ALIASES = {
    "angel diamond tags": "Angel Diamond Tag",
    "angel gold tag": "Angel Gold Tag",
    "angel silver tag": "Angel Silver Tag",
    "angel simple trigger mechanisms": "Angel Simple Trigger Mechanism",
    "prototype cloaking device": "Prototype Cloaking Device I",
    "talocan sketch book": "Talocan Sketch Books",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    print(message, flush=True)


def fetch_bytes(url: str, *, method: str = "GET", retries: int = 3, timeout: int = 120) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - retried below
            last_error = exc
            if attempt < retries:
                wait = attempt * 2
                log(f"  retry {attempt}/{retries - 1} for {url} after {wait}s: {exc}")
                time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error


def fetch_json(url: str, *, retries: int = 3, timeout: int = 120) -> Any:
    return json.loads(fetch_bytes(url, retries=retries, timeout=timeout).decode("utf-8"))


def fetch_last_modified(url: str) -> str | None:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.headers.get("Last-Modified")


def ensure_types_csv(cache_dir: Path) -> Path:
    path = cache_dir / "invTypes.csv"
    # Refresh weekly; the SDE changes infrequently.
    if path.exists() and (time.time() - path.stat().st_mtime) < 7 * 86400:
        return path
    cache_dir.mkdir(parents=True, exist_ok=True)
    log(f"Downloading Fuzzwork SDE types -> {path}")
    data = fetch_bytes(FUZZWORK_TYPES_URL, timeout=300)
    path.write_bytes(data)
    return path


def load_type_ids(cache_dir: Path) -> dict[str, int]:
    csv_path = ensure_types_csv(cache_dir)
    exact: dict[str, int] = {}
    folded: dict[str, int] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                type_id = int(row["typeID"])
            except (TypeError, ValueError):
                continue
            name = (row.get("typeName") or "").strip()
            if not name:
                continue
            exact[name] = type_id
            folded[name.casefold()] = type_id
    return {
        "exact": exact,
        "folded": folded,
    }


def resolve_type_id(name: str, types: dict[str, dict[str, int]]) -> int | None:
    candidate = re.sub(r"\s+", " ", name).strip()
    if candidate in types["exact"]:
        return types["exact"][candidate]

    folded = candidate.casefold()
    if folded in types["folded"]:
        return types["folded"][folded]

    alias = ITEM_ALIASES.get(folded)
    if alias:
        if alias in types["exact"]:
            return types["exact"][alias]
        if alias.casefold() in types["folded"]:
            return types["folded"][alias.casefold()]

    # A few fallbacks for simple plural/case differences.
    variants = {
        folded.rstrip("s"),
        folded.replace(" tags", " tag"),
        folded.replace(" tag", " tags"),
        folded.replace("mechanisms", "mechanism"),
        folded.replace("mechanism", "mechanisms"),
    }
    for variant in variants:
        if variant in types["folded"]:
            return types["folded"][variant]

    return None


def parse_quantity(name: str) -> tuple[str, int]:
    match = re.search(r"\s*\((\d[\d,]*)\)\s*$", name)
    if not match:
        return name.strip(), 1
    quantity = int(match.group(1).replace(",", ""))
    return name[: match.start()].strip(), max(quantity, 1)


def strip_tags(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def parse_shopping_rows(repo_root: Path) -> dict[str, list[dict[str, Any]]]:
    """Return {line_slug: [row, ...]} from cosmos/*-cosmos.html."""
    lines: dict[str, list[dict[str, Any]]] = {}
    for path in sorted((repo_root / "cosmos").glob("*-cosmos.html")):
        slug = path.stem.removesuffix("-cosmos")
        source = path.read_text(encoding="utf-8")
        rows: list[dict[str, Any]] = []
        for table in re.findall(r'<table data-kind="item"[^>]*>(.*?)</table>', source, re.S):
            for row_match in re.finditer(r"<tr\b([^>]*)>(.*?)</tr>", table, re.S):
                attrs, body = row_match.groups()
                name_match = re.search(r'data-name="([^"]*)"', attrs)
                item_match = re.search(
                    r'<td class="main" data-label="Item">(.*?)</td>', body, re.S
                )
                if not name_match and not item_match:
                    continue

                raw_name = html.unescape(name_match.group(1) if name_match else "")
                if not raw_name and item_match:
                    raw_name = strip_tags(item_match.group(1))
                    raw_name = re.sub(r"\s+(Market|Contracts)$", "", raw_name).strip()
                if not raw_name:
                    continue

                item_cell = item_match.group(1) if item_match else ""
                name, quantity = parse_quantity(raw_name)

                channel = (re.search(r'data-channel="([^"]*)"', attrs) or [None, ""])[1].strip().lower()
                if not channel:
                    if 'class="tag src"' in item_cell and ">Market<" in item_cell:
                        channel = "market"
                    elif 'class="tag src alt"' in item_cell or ">Contracts<" in item_cell:
                        channel = "contracts"

                required_match = re.search(r'data-required-for="([^"]*)"', attrs)
                required_for = [
                    part.strip()
                    for part in (required_match.group(1) if required_match else "").split("|")
                    if part.strip()
                ]

                rows.append(
                    {
                        "name": name,
                        "quantity": quantity,
                        "channel": channel or None,
                        "required_for": required_for,
                    }
                )
        if rows:
            lines[slug] = rows
    return lines


def fetch_market_prices(type_ids: list[int]) -> dict[int, float]:
    prices: dict[int, float] = {}
    unique_ids = sorted(set(type_ids))
    for start in range(0, len(unique_ids), 100):
        batch = unique_ids[start : start + 100]
        url = (
            f"{FUZZWORK_AGGREGATES_URL}?station={JITA_STATION_ID}&types="
            + ",".join(str(type_id) for type_id in batch)
        )
        try:
            payload = fetch_json(url)
        except Exception as exc:  # noqa: BLE001
            log(f"  warning: Fuzzwork aggregates batch failed: {exc}")
            continue
        for type_id in batch:
            entry = payload.get(str(type_id)) or {}
            sell = entry.get("sell") or {}
            value = None
            for key in ("percentile", "min"):
                try:
                    candidate = float(sell.get(key) or 0)
                except (TypeError, ValueError):
                    candidate = 0
                if candidate > 0:
                    value = candidate
                    break
            if value is not None:
                prices[type_id] = value
    return prices


def load_contract_prices(cache_dir: Path, type_ids: set[int], scrape_end: str) -> dict[int, float]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / "public-contracts-latest.tar.bz2"
    log(f"Downloading EVE Ref contract snapshot -> {archive_path}")
    archive_path.write_bytes(fetch_bytes(EVEREF_CONTRACTS_URL, timeout=300))

    contracts: dict[str, dict[str, str]] = {}
    target_items: dict[str, dict[int, int]] = {}
    contract_item_types: dict[str, set[int]] = {}

    with tarfile.open(archive_path, "r:bz2") as archive:
        meta_member = archive.extractfile("meta.json")
        meta = json.loads(meta_member.read().decode("utf-8")) if meta_member else {}
        effective_scrape_end = meta.get("scrape_end") or scrape_end

        contracts_member = archive.extractfile("contracts.csv")
        if contracts_member is None:
            raise RuntimeError("contracts.csv missing from EVE Ref archive")
        contracts_reader = csv.DictReader(io.TextIOWrapper(contracts_member, encoding="utf-8"))
        for row in contracts_reader:
            if (row.get("type") or "") != "item_exchange":
                continue
            try:
                price = float(row.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            expired = (row.get("date_expired") or "").strip()
            if expired and effective_scrape_end and expired <= effective_scrape_end:
                continue
            contract_id = (row.get("contract_id") or "").strip()
            if not contract_id:
                continue
            contracts[contract_id] = {
                "price": row.get("price") or "0",
                "region_id": (row.get("region_id") or "").strip(),
            }

        items_member = archive.extractfile("contract_items.csv")
        if items_member is None:
            raise RuntimeError("contract_items.csv missing from EVE Ref archive")
        items_reader = csv.DictReader(io.TextIOWrapper(items_member, encoding="utf-8"))
        for row in items_reader:
            contract_id = (row.get("contract_id") or "").strip()
            if contract_id not in contracts:
                continue
            if (row.get("is_included") or "").strip().lower() != "true":
                continue
            try:
                type_id = int(row.get("type_id") or 0)
            except (TypeError, ValueError):
                continue
            try:
                quantity = int(float(row.get("quantity") or 0))
            except (TypeError, ValueError):
                continue
            if type_id <= 0 or quantity <= 0:
                continue

            contract_item_types.setdefault(contract_id, set()).add(type_id)
            if type_id in type_ids:
                target_items.setdefault(contract_id, {})
                target_items[contract_id][type_id] = target_items[contract_id].get(type_id, 0) + quantity

    forge_prices: dict[int, list[float]] = {}
    all_prices: dict[int, list[float]] = {}

    for contract_id, items in target_items.items():
        if len(contract_item_types.get(contract_id, set())) != 1 or len(items) != 1:
            continue
        contract = contracts[contract_id]
        try:
            price = float(contract["price"])
        except (TypeError, ValueError):
            continue
        type_id, quantity = next(iter(items.items()))
        if price <= 0 or quantity <= 0:
            continue
        unit = price / quantity
        if unit <= 0:
            continue
        all_prices.setdefault(type_id, []).append(unit)
        if contract["region_id"] == str(THE_FORGE_REGION_ID):
            forge_prices.setdefault(type_id, []).append(unit)

    def percentile_5(values: list[float]) -> float:
        values = sorted(values)
        if len(values) < 5:
            return values[0]
        index = max(0, min(len(values) - 1, math.floor((len(values) - 1) * 0.05)))
        return values[index]

    prices: dict[int, float] = {}
    for type_id in type_ids:
        values = forge_prices.get(type_id) or all_prices.get(type_id)
        if values:
            prices[type_id] = percentile_5(values)
    return prices


def build_costs(
    repo_root: Path,
    cache_dir: Path,
    *,
    force: bool,
) -> dict[str, Any] | None:
    out_path = repo_root / "cosmos" / "data" / "costs.json"
    existing: dict[str, Any] = {}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}

    today = utc_now().date().isoformat()
    snapshot = existing.get("snapshot") or {}
    if not force and snapshot.get("lastRunDate") == today:
        log(f"Already processed today ({today}); skipping. Use --force to override.")
        return None

    log("Checking EVE Ref snapshot timestamp...")
    last_modified = fetch_last_modified(EVEREF_CONTRACTS_URL)
    if not force and last_modified and snapshot.get("everefLastModified") == last_modified:
        log(f"EVE Ref snapshot unchanged ({last_modified}); skipping. Use --force to override.")
        return None

    rows_by_line = parse_shopping_rows(repo_root)
    if not rows_by_line:
        raise RuntimeError("No COSMOS shopping rows found")

    log("Loading Fuzzwork type IDs...")
    types = load_type_ids(cache_dir)

    resolved: dict[str, int | None] = {}
    for rows in rows_by_line.values():
        for row in rows:
            name = row["name"]
            if name not in resolved:
                resolved[name] = resolve_type_id(name, types)

    all_type_ids = sorted({type_id for type_id in resolved.values() if type_id})
    log(f"Resolved {sum(1 for v in resolved.values() if v)} of {len(resolved)} item names")

    log("Fetching Fuzzwork market aggregates...")
    market_prices = fetch_market_prices(all_type_ids)
    log(f"Fuzzwork prices: {len(market_prices)}/{len(all_type_ids)}")

    log("Building EVE Ref contract prices...")
    contract_prices = load_contract_prices(cache_dir, set(all_type_ids), iso_now())
    log(f"EVE Ref prices: {len(contract_prices)}/{len(all_type_ids)}")

    try:
        orderset = fetch_json(FUZZWORK_ORDERSET_URL)
        if isinstance(orderset, dict):
            orderset = orderset.get("orderset")
    except Exception:  # noqa: BLE001
        orderset = None

    lines: dict[str, Any] = {}
    for slug, rows in sorted(rows_by_line.items()):
        total = 0.0
        priced_items = 0
        unpriced: list[str] = []
        for row in rows:
            type_id = resolved.get(row["name"])
            market = market_prices.get(type_id) if type_id else None
            contract = contract_prices.get(type_id) if type_id else None
            channel = row.get("channel")
            price: float | None = None
            if channel == "market":
                price = market or contract
            elif channel == "contracts":
                price = contract or market
            else:
                candidates = [value for value in (market, contract) if value is not None]
                price = min(candidates) if candidates else None

            if price is None:
                if row["name"] not in unpriced:
                    unpriced.append(row["name"])
                continue

            total += price * row["quantity"]
            priced_items += 1

        lines[slug] = {
            "shopping": {
                "total": round(total, 2),
                "pricedItems": priced_items,
                "totalItems": len(rows),
                "unpriced": sorted(unpriced),
                "updated": iso_now(),
            }
        }

    payload = {
        "generatedAt": iso_now(),
        "sources": {
            "market": "Fuzzwork aggregates (Jita 4-4, station 60003760)",
            "contracts": "EVE Ref public contract snapshot",
            "types": "Fuzzwork SDE invTypes.csv",
        },
        "snapshot": {
            "lastRunDate": today,
            "everefLastModified": last_modified,
            "everefScrapeEnd": None,
            "fuzzworkOrderset": orderset,
        },
        "lines": lines,
    }

    # Best-effort scrape_end from the archive metadata, for diagnostics.
    archive_path = cache_dir / "public-contracts-latest.tar.bz2"
    try:
        with tarfile.open(archive_path, "r:bz2") as archive:
            meta_member = archive.extractfile("meta.json")
            if meta_member:
                meta = json.loads(meta_member.read().decode("utf-8"))
                payload["snapshot"]["everefScrapeEnd"] = meta.get("scrape_end")
    except Exception:  # noqa: BLE001
        pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    log(f"Wrote {out_path}")
    for slug, line in lines.items():
        info = line["shopping"]
        log(
            f"  {slug}: {info['pricedItems']}/{info['totalItems']} items priced, "
            f"{info['total']:,.0f} ISK"
        )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--force", action="store_true", help="Ignore the once-per-day guard")
    args = parser.parse_args()
    root = args.root.resolve()
    cache_dir = (args.cache_dir or (root / ".cache")).resolve()
    try:
        build_costs(root, cache_dir, force=args.force)
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
