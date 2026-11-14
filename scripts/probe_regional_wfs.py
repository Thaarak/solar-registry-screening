#!/usr/bin/env python3
"""
Probe regional WFS endpoints to confirm per-installation solar data actually exists.

This is a READ-ONLY diagnostic. It touches no database and writes no coordinates.
It checks whether a public regional GIS endpoint offers installation data
with a registry identifier that can be joined to source records.

For each region it does, per candidate endpoint:
  1. GetCapabilities  -> lists the published FeatureTypes and flags the ones whose
                         name/title/abstract look like energy / solar / installations.
  2. GetFeature (n=1) -> on each flagged layer (with --sample), dumps the property keys
                         and one sample feature, detects whether a geometry is present,
                         and guesses which property is the registry-id join key.

The output tells you, per region, one of:
  - DATA FOUND      : reachable + a layer with geometry + (ideally) a registry-id field
  - NO MATCHING LAYER : reachable, but nothing solar/energy-shaped is published here
  - UNREACHABLE     : host unreachable from this network

Use the dumped property keys to fill REGIONAL_SOURCES[region]['wfs']['layers' / 'field_map']
in src/enrichment/regional_registry.py, then run scripts/enrich_regional.py --source wfs.

Usage:
  python scripts/probe_regional_wfs.py
  python scripts/probe_regional_wfs.py --regions ANDALUCIA,MURCIA --sample
  python scripts/probe_regional_wfs.py --region CASTILLA_LA_MANCHA \
      --endpoint https://some-ide-host/geoserver/wfs --sample
  python scripts/probe_regional_wfs.py --list-all-layers          # don't keyword-filter
For endpoint details, consult the relevant regional open-data catalog and
follow that provider's access and redistribution terms.

We only have a confirmed endpoint for Andalucia (in REGIONAL_SOURCES). Murcia's
configured host (iderm.carm.es) does not even resolve, and CLM / Extremadura /
Castilla y Leon have no endpoint yet. To find the real ones:
  1. Open the region's IDE / energy map viewer in a browser (on Spanish egress).
  2. Open DevTools -> Network, then pan/zoom the map so layers load.
  3. Copy a live request URL ending in `?service=WFS&request=GetFeature`
     (or `...request=GetMap` for WMS) — its host+path is the base endpoint.
  4. Paste that base URL into EXTRA_CANDIDATES[<REGION>] below and re-run --sample.
IMPORTANT: a "NO MATCHING LAYER" verdict against a *wrong/stale* URL does NOT mean
the data is absent — confirm the endpoint via step 3 before concluding anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from xml.etree import ElementTree

import requests

# Make `src` importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.enrichment.regional_registry import DEFAULT_REGIONS, REGIONAL_SOURCES  # noqa: E402


# ---------------------------------------------------------------------------
# Candidate endpoints
# ---------------------------------------------------------------------------
# The probe tests every endpoint configured in REGIONAL_SOURCES PLUS any extras
# listed here. For the 4 regions whose endpoint is still unknown, paste WFS base
# URLs you find in that community's IDE / open-data catalog into the lists below
# and re-run. (Left intentionally empty rather than guessed — a wrong URL just
# wastes a round trip. The catalogs to search are named in each comment.)
EXTRA_CANDIDATES: dict[str, list[str]] = {
    # IDEAndalucía / Agencia Andaluza de la Energía. AAE base is already in config.
    'ANDALUCIA': [],
    # IDERM (CARM). The host in REGIONAL_SOURCES (iderm.carm.es) did NOT resolve in
    # test runs — treat it as a stale placeholder, not a geoblock. Re-discover the
    # real IDERM/SITMURCIA WFS base via the portal Network tab and paste it here.
    'MURCIA': [],
    # IDE Castilla-La Mancha (search: catalogo IDE JCCM / geoportal CLM).
    'CASTILLA_LA_MANCHA': [],
    # IDEEX / Extremadura (search: IDE Extremadura geoportal / GeoServer).
    'EXTREMADURA': [],
    # IDECyL (search: idecyl.jcyl.es services).
    'CASTILLA_Y_LEON': [],
}

# Layers whose name/title/abstract contain any of these are flagged as candidates.
ENERGY_KEYWORDS = [
    'solar', 'fotovolt', 'fotovoltaic', 'fv', 'renovab', 'energia', 'energía',
    'energet', 'instalac', 'generac', 'planta', 'autoconsumo', 'electric',
]

# Property keys containing any of these are guessed to be the registry-id join key.
REF_KEYWORDS = [
    'registro', 'expediente', 'rae', 'ref', 'num', 'codigo', 'código', 'clave', 'id',
]

# Output formats to try for the GetFeature sample, in order of preference.
JSON_OUTPUT_FORMATS = ['application/json', 'json', 'geojson', 'application/geo+json']

WFS_VERSIONS = ['2.0.0', '1.1.0']


def local(tag: str) -> str:
    """Strip the XML namespace from a tag, e.g. '{ns}FeatureType' -> 'FeatureType'."""
    return tag.rsplit('}', 1)[-1]


# A current mainstream desktop Chrome UA + Spanish Accept-Language. Some WAFs drop
# requests whose Accept-Language is missing or non-local; this is honest local-user
# signalling, not bot/TLS evasion (which our connect-timeout + DNS symptoms wouldn't
# benefit from anyway).
BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
    'Accept': 'application/xml,application/json,text/xml,*/*;q=0.8',
}


def session(proxy: str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    if proxy:
        s.proxies.update({'http': proxy, 'https': proxy})
    return s


# ---------------------------------------------------------------------------
# GetCapabilities
# ---------------------------------------------------------------------------

def get_capabilities(sess: requests.Session, endpoint: str, timeout: int):
    """
    Return (version, list_of_feature_types) or raise a clear exception.

    feature_type = {'name': str, 'title': str, 'abstract': str}
    """
    last_err: Exception | None = None
    for version in WFS_VERSIONS:
        params = {'service': 'WFS', 'version': version, 'request': 'GetCapabilities'}
        try:
            resp = sess.get(endpoint, params=params, timeout=timeout)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.content)
            fts = parse_feature_types(root)
            # A valid capabilities doc with parsable FeatureTypes wins.
            return version, fts
        except ElementTree.ParseError as e:
            last_err = ValueError(f"non-XML response for v{version}: {e}")
        except requests.RequestException as e:
            last_err = e
    assert last_err is not None
    raise last_err


def parse_feature_types(root: ElementTree.Element) -> list[dict]:
    out: list[dict] = []
    for el in root.iter():
        if local(el.tag) != 'FeatureType':
            continue
        name = title = abstract = ''
        keywords: list[str] = []
        for child in el:
            lname = local(child.tag)
            if lname == 'Name':
                name = (child.text or '').strip()
            elif lname == 'Title':
                title = (child.text or '').strip()
            elif lname == 'Abstract':
                abstract = (child.text or '').strip()
            elif lname in ('Keywords', 'ows:Keywords'):
                keywords.extend((k.text or '').strip() for k in child.iter() if (k.text or '').strip())
        if name:
            out.append({
                'name': name,
                'title': title,
                'abstract': abstract,
                'keywords': ' '.join(keywords),
            })
    return out


def looks_energy(ft: dict) -> bool:
    haystack = ' '.join([ft['name'], ft['title'], ft['abstract'], ft['keywords']]).lower()
    return any(kw in haystack for kw in ENERGY_KEYWORDS)


# ---------------------------------------------------------------------------
# GetFeature sample
# ---------------------------------------------------------------------------

def get_feature_sample(sess: requests.Session, endpoint: str, version: str,
                       type_name: str, timeout: int):
    """
    Fetch one feature as GeoJSON. Returns (properties: dict, has_geometry: bool)
    or (None, False) if no JSON output format is supported / nothing returned.
    """
    count_key = 'count' if version.startswith('2') else 'maxFeatures'
    type_key = 'typeNames' if version.startswith('2') else 'typeName'
    for out_fmt in JSON_OUTPUT_FORMATS:
        params = {
            'service': 'WFS',
            'version': version,
            'request': 'GetFeature',
            type_key: type_name,
            count_key: '1',
            'outputFormat': out_fmt,
            'srsName': 'EPSG:4326',
        }
        try:
            resp = sess.get(endpoint, params=params, timeout=timeout)
            if resp.status_code != 200:
                continue
            data = resp.json()
        except (requests.RequestException, ValueError):
            continue
        feats = data.get('features') or []
        if not feats:
            # Valid JSON but empty: layer exists but has no rows.
            return {}, False
        feat = feats[0]
        props = feat.get('properties', {}) or {}
        has_geom = bool(feat.get('geometry'))
        return props, has_geom
    return None, False


def guess_ref_keys(props: dict) -> list[str]:
    keys = []
    for k in props:
        kl = k.lower()
        if any(rk in kl for rk in REF_KEYWORDS):
            keys.append(k)
    return keys


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def candidate_endpoints(region: str) -> list[str]:
    eps: list[str] = []
    cfg_ep = REGIONAL_SOURCES.get(region, {}).get('wfs', {}).get('endpoint')
    if cfg_ep:
        eps.append(cfg_ep)
    for ep in EXTRA_CANDIDATES.get(region, []):
        if ep and ep not in eps:
            eps.append(ep)
    return eps


def probe_region(sess: requests.Session, region: str, endpoints: list[str],
                 args) -> str:
    """Probe all endpoints for a region. Returns a verdict string."""
    print(f"\n{'=' * 70}\nREGION: {region}\n{'=' * 70}")
    if not endpoints:
        print("  (no candidate endpoint configured — add one to EXTRA_CANDIDATES "
              "or pass --endpoint)")
        return 'NO_ENDPOINT'

    region_verdict = 'NO MATCHING LAYER'
    any_reachable = False

    for endpoint in endpoints:
        print(f"\n  endpoint: {endpoint}")
        try:
            version, fts = get_capabilities(sess, endpoint, args.timeout)
        except requests.exceptions.ConnectionError as e:
            print(f"    UNREACHABLE (connection refused/blocked — likely geofenced): {e}")
            continue
        except requests.exceptions.Timeout:
            print(f"    UNREACHABLE (timeout after {args.timeout}s — likely geofenced)")
            continue
        except requests.RequestException as e:
            print(f"    ERROR (HTTP): {e}")
            continue
        except ValueError as e:
            print(f"    ERROR (bad capabilities doc): {e}")
            continue

        any_reachable = True
        print(f"    REACHABLE — WFS {version}, {len(fts)} layers published")

        candidates = fts if args.list_all_layers else [ft for ft in fts if looks_energy(ft)]
        if not candidates:
            print("    no energy/solar-looking layer found "
                  "(re-run with --list-all-layers to see everything)")
            continue

        label = "all layers" if args.list_all_layers else "energy/solar candidate layers"
        print(f"    {len(candidates)} {label}:")
        for ft in candidates:
            title = f" — {ft['title']}" if ft['title'] else ""
            print(f"      • {ft['name']}{title}")

            if not args.sample:
                continue

            props, has_geom = get_feature_sample(
                sess, endpoint, version, ft['name'], args.timeout
            )
            if props is None:
                print("          sample: no JSON output format supported "
                      "(try GML manually / check outputFormats in capabilities)")
                continue
            if not props and not has_geom:
                print("          sample: layer is EMPTY (0 features)")
                continue

            geom_flag = "yes" if has_geom else "NO GEOMETRY"
            ref_keys = guess_ref_keys(props)
            print(f"          geometry: {geom_flag}")
            print(f"          properties ({len(props)}): {', '.join(list(props.keys())[:25])}")
            if ref_keys:
                print(f"          likely registry-id key(s): {', '.join(ref_keys)}")
            else:
                print("          likely registry-id key(s): none obvious "
                      "(inspect the sample below)")
            if args.verbose:
                print("          sample feature properties:")
                print("          " + json.dumps(props, ensure_ascii=False, indent=2)
                      .replace("\n", "\n          "))

            if has_geom:
                region_verdict = 'DATA FOUND'

    if not any_reachable:
        return 'UNREACHABLE'
    return region_verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--regions', default=','.join(DEFAULT_REGIONS),
                        help='comma-separated regions to probe (default: all 5 gap regions)')
    parser.add_argument('--endpoint', default=None,
                        help='extra WFS base URL to test (use with --region)')
    parser.add_argument('--region', default=None,
                        help='region the --endpoint belongs to')
    parser.add_argument('--sample', action='store_true',
                        help='also GetFeature(n=1) on candidate layers to inspect fields')
    parser.add_argument('--list-all-layers', action='store_true',
                        help='do not keyword-filter; list every published layer')
    parser.add_argument('--timeout', type=int, default=30,
                        help='per-request timeout in seconds (default: 30)')
    parser.add_argument('--proxy', default=None,
                        help='optional network proxy (SOCKS requires PySocks)')
    parser.add_argument('--verbose', action='store_true',
                        help='print full sample feature properties as JSON')
    args = parser.parse_args()

    # SOCKS proxies require PySocks; fail with a clear hint rather than a traceback.
    if args.proxy and args.proxy.lower().startswith('socks'):
        try:
            import socks  # noqa: F401  (PySocks)
        except ImportError:
            parser.error("a socks:// proxy requires PySocks — install it with "
                         "`venv/bin/pip install PySocks` (or use an http:// proxy)")

    regions = [r.strip().upper() for r in args.regions.split(',') if r.strip()]

    # Allow injecting an ad-hoc endpoint for one region.
    if args.endpoint:
        if not args.region:
            parser.error('--endpoint requires --region')
        region = args.region.strip().upper()
        EXTRA_CANDIDATES.setdefault(region, []).append(args.endpoint)
        if region not in regions:
            regions.append(region)

    if args.proxy:
        print("Using an explicitly configured network proxy")
    sess = session(args.proxy)
    verdicts: dict[str, str] = {}
    for region in regions:
        verdicts[region] = probe_region(sess, region, candidate_endpoints(region), args)

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    for region in regions:
        print(f"  {region:<20} {verdicts[region]}")
    print()
    print("Legend: DATA FOUND = reachable layer with geometry (fill REGIONAL_SOURCES "
          "and run enrich_regional.py --source wfs)")
    print("        NO MATCHING LAYER = reachable but nothing solar/energy-shaped here")
    print("        UNREACHABLE = blocked/down from this network (retry under VPN / EU cloud)")
    print("        NO_ENDPOINT = add a candidate URL to EXTRA_CANDIDATES or pass --endpoint")

    # Exit non-zero if nothing was confirmed, so this can gate a CI/cloud run.
    return 0 if 'DATA FOUND' in verdicts.values() else 1


if __name__ == '__main__':
    raise SystemExit(main())
