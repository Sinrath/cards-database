#!/usr/bin/env python3
"""Audit the cardmarket ids in a cards-database set against Cardmarket's own data.

Cardmarket does not sell holo and reverse holo as separate products: one product
carries both, as two price series on the same idProduct --

    avg        -> the Holo
    avg-holo   -> the Reverse Holo

So a product with a populated `avg-holo` HAS a reverse printing (it is the card
you pull from a pack), and one with `avg-holo: null` has a single printing --
a secret rare, a promo, or a Build & Battle non-holo. That is the check this
script leans on, alongside the product's expansion id.

Usage
-----
    python3 audit-cardmarket-ids.py "Mega Evolution/Chaos Rising"
    python3 audit-cardmarket-ids.py "Mega Evolution/Pitch Black" --repo /path/to/cards-database
    python3 audit-cardmarket-ids.py "Mega Evolution/Perfect Order" --refresh

Exit code is 0 when no ERRORs were found, 1 otherwise. Warnings do not fail it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request

CATALOG_URL = "https://downloads.s3.cardmarket.com/productCatalog/productList/products_singles_6.json"
PRICES_URL = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_6.json"
CACHE_DIR = os.path.expanduser("~/.cache/cardmarket")
MAX_AGE_H = 24
# the repo root is the parent of the directory holding this script
DEFAULT_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

Q = r'["\']([^"\']+)["\']'


def fetch(url: str, name: str, refresh: bool) -> dict:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, name)
    fresh = os.path.exists(path) and (time.time() - os.path.getmtime(path)) < MAX_AGE_H * 3600
    if refresh or not fresh:
        print("  downloading %s ..." % name, file=sys.stderr)
        urllib.request.urlretrieve(url, path)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def bracket_block(src: str, start_idx: int) -> str:
    """Return the balanced [...] block beginning at start_idx."""
    depth = 0
    for j in range(start_idx, len(src)):
        if src[j] == "[":
            depth += 1
        elif src[j] == "]":
            depth -= 1
            if depth == 0:
                return src[start_idx:j + 1]
    return ""


def split_objects(block: str) -> list[str]:
    depth, start, out = 0, None, []
    for j, ch in enumerate(block):
        if ch == "{":
            if depth == 0:
                start = j
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                out.append(block[start:j + 1])
    return out


def parse_card(path: str):
    src = open(path, encoding="utf-8").read()
    nm = re.search(r"\n\tname: \{\s*\n\t\ten: " + Q, src)
    rar = re.search(r"\n\trarity: " + Q, src)
    m = re.search(r"\n\tvariants: \[", src)
    variants = []
    if m:
        for obj in split_objects(bracket_block(src, m.end() - 1)):
            vtype = re.search(r"type:\s*" + Q, obj)
            foil = re.search(r"foil:\s*" + Q, obj)
            sm = re.search(r"stamp:\s*\[(.*?)\]", obj, re.S)
            cm = re.search(r"cardmarket:\s*(\d+)", obj)
            variants.append({
                "type": vtype.group(1) if vtype else "?",
                "foil": foil.group(1) if foil else "",
                "stamp": "+".join(re.findall(Q, sm.group(1))) if sm else "",
                "cardmarket": int(cm.group(1)) if cm else None,
            })
    return {
        "name": nm.group(1) if nm else "?",
        "rarity": rar.group(1) if rar else "?",
        "variants": variants,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("set", help='set path under data/, e.g. "Mega Evolution/Chaos Rising"')
    ap.add_argument("--repo", default=DEFAULT_REPO, help="cards-database checkout (default: %(default)s)")
    ap.add_argument("--refresh", action="store_true", help="force re-download of the Cardmarket files")
    args = ap.parse_args()

    set_file = os.path.join(args.repo, "data", args.set + ".ts")
    card_dir = os.path.join(args.repo, "data", args.set)
    if not os.path.isfile(set_file) or not os.path.isdir(card_dir):
        print("cannot find set at %s{,.ts}" % card_dir, file=sys.stderr)
        return 2

    exp_m = re.search(r"cardmarket:\s*(\d+)", open(set_file, encoding="utf-8").read())
    if not exp_m:
        print("set file has no thirdParty.cardmarket expansion id", file=sys.stderr)
        return 2
    set_exp = int(exp_m.group(1))

    catalog = fetch(CATALOG_URL, "products_singles_6.json", args.refresh)
    prices = fetch(PRICES_URL, "price_guide_6.json", args.refresh)
    products = {p["idProduct"]: p for p in catalog["products"]}
    guide = {p["idProduct"]: p for p in prices["priceGuides"]}

    print("%s  (expansion %d)" % (args.set, set_exp))

    errors, warnings = [], []
    owner: dict[int, str] = {}          # cardmarket id -> card that first claimed it

    for fn in sorted(f for f in os.listdir(card_dir) if f.endswith(".ts")):
        num = fn[:-3]
        card = parse_card(os.path.join(card_dir, fn))
        # A `normal` alongside a plain `holo` is an extra printing (Build & Battle
        # or a promo), not the pack card -- that is the only case where a `normal`
        # pointing at a product with a reverse series is suspicious.
        has_plain_holo = any(v["type"] == "holo" and not v["foil"] and not v["stamp"]
                             for v in card["variants"])
        for v in card["variants"]:
            cid = v["cardmarket"]
            if cid is None:
                continue
            plain = not v["stamp"] and not v["foil"]
            label = "%s %-26s %-7s%s" % (num, card["name"], v["type"],
                                         ("/" + v["stamp"]) if v["stamp"] else "")

            prod = products.get(cid)
            if prod is None:
                errors.append("MISSING     %s  id=%d is not in the Cardmarket catalog" % (label, cid))
                continue

            has_reverse = guide.get(cid, {}).get("avg-holo") is not None

            # a plain holo/reverse must be a product from this set's own expansion
            if plain and v["type"] in ("holo", "reverse") and prod["idExpansion"] != set_exp:
                errors.append("WRONG-EXP   %s  id=%d lives in expansion %d, set is %d (%s)"
                              % (label, cid, prod["idExpansion"], set_exp, prod["name"]))

            # a reverse can only exist on a product that actually has a reverse series
            if plain and v["type"] == "reverse" and not has_reverse:
                errors.append("NO-REVERSE  %s  id=%d has no avg-holo, so it has no reverse printing (%s)"
                              % (label, cid, prod["name"]))

            # an extra printing (Build & Battle / promo normal) should be a
            # single-printing product, not the pack card
            if v["type"] == "normal" and has_plain_holo and has_reverse:
                warnings.append("EXTRA-ID?   %s  id=%d has a reverse series, so it is the pack card, not the extra printing"
                                % (label, cid))

            # the same id on two different cards is always wrong
            prev = owner.get(cid)
            if prev is not None and prev != num:
                errors.append("SHARED-ID   %s  id=%d is already used by card %s (%s)"
                              % (label, cid, prev, prod["name"]))
            else:
                owner.setdefault(cid, num)

            # names differ legitimately between tcgdex and Cardmarket, so only warn
            if not prod["name"].lower().startswith(card["name"].lower()[:6]):
                warnings.append("NAME?       %s  id=%d -> %s" % (label, cid, prod["name"]))

    for e in errors:
        print("  " + e)
    for w in warnings:
        print("  " + w)
    print("\n  %d error(s), %d warning(s)" % (len(errors), len(warnings)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
