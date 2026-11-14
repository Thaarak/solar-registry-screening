#!/usr/bin/env python3
"""
Validate access to Spain's PRETOR (MITECO) registry data source.

This script tests:
1. MITECO portal accessibility
2. PRETOR registry download availability
3. XLS file parsing capability
4. Available fields and format

Usage:
    python scripts/validate_pretor.py
"""

import sys
import os
import tempfile
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: requests not installed. Run: pip install requests")
    sys.exit(1)

# PRETOR registry URLs
PRETOR_URLS = {
    'miteco_energia': 'https://www.miteco.gob.es/es/energia/energia-electrica/electricidad/energias-renovables.html',
    'pretor_info': 'https://sede.serviciosmin.gob.es/es-ES/procedimientoselectronicos/Paginas/Energia.aspx',
    'registro_instalaciones': 'https://www.miteco.gob.es/es/energia/energia-electrica/electricidad/energias-renovables/registro-administrativo.html',
}

# Expected data fields in PRETOR registry
EXPECTED_FIELDS = [
    'instalacion',           # Installation name
    'potencia_instalada',    # Installed capacity
    'tecnologia',            # Technology type
    'provincia',             # Province
    'comunidad_autonoma',    # Autonomous community
    'fecha_inscripcion',     # Registration date
    'estado',                # Status (preliminary/definitive)
]


def test_url_accessibility(name: str, url: str) -> dict:
    """Test if a URL is accessible."""
    result = {
        'name': name,
        'url': url,
        'accessible': False,
        'status_code': None,
        'content_type': None,
        'error': None,
        'has_download_links': False,
    }

    try:
        print(f"  Testing {name}...")
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }
        response = requests.get(url, timeout=30, allow_redirects=True, headers=headers)
        result['status_code'] = response.status_code
        result['content_type'] = response.headers.get('Content-Type', 'unknown')
        result['accessible'] = response.status_code == 200

        if result['accessible']:
            # Check for download links (XLS, XLSX, CSV)
            content_lower = response.text.lower()
            if '.xls' in content_lower or '.xlsx' in content_lower or '.csv' in content_lower:
                result['has_download_links'] = True
                print(f"    [OK] Status: {response.status_code}, Has download links: Yes")
            else:
                print(f"    [OK] Status: {response.status_code}, Has download links: No")
        else:
            print(f"    [FAIL] Status: {response.status_code}")

    except requests.exceptions.Timeout:
        result['error'] = 'Connection timeout'
        print(f"    [FAIL] Timeout")
    except requests.exceptions.ConnectionError as e:
        result['error'] = f'Connection error: {str(e)[:100]}'
        print(f"    [FAIL] Connection error")
    except Exception as e:
        result['error'] = str(e)[:200]
        print(f"    [FAIL] Error: {result['error']}")

    return result


def test_xls_parsing() -> dict:
    """Test XLS file parsing capability."""
    result = {
        'xlrd_available': False,
        'openpyxl_available': False,
        'pandas_available': False,
        'can_parse_legacy_xls': False,
        'can_parse_xlsx': False,
    }

    print("  Testing XLS parsing libraries...")

    # Test xlrd (for legacy .xls)
    try:
        import xlrd
        result['xlrd_available'] = True
        result['can_parse_legacy_xls'] = True
        print(f"    [OK] xlrd available (version {xlrd.__VERSION__})")
    except ImportError:
        print(f"    [WARN] xlrd not installed - needed for legacy .xls files")

    # Test openpyxl (for .xlsx)
    try:
        import openpyxl
        result['openpyxl_available'] = True
        result['can_parse_xlsx'] = True
        print(f"    [OK] openpyxl available (version {openpyxl.__version__})")
    except ImportError:
        print(f"    [WARN] openpyxl not installed - needed for .xlsx files")

    # Test pandas (preferred interface)
    try:
        import pandas
        result['pandas_available'] = True
        print(f"    [OK] pandas available (version {pandas.__version__})")
    except ImportError:
        print(f"    [WARN] pandas not installed - recommended for data processing")

    return result


def search_for_download_links(url: str) -> list:
    """Search a page for downloadable data files."""
    links = []

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }
        response = requests.get(url, timeout=30, headers=headers)

        if response.status_code == 200:
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(response.text, 'html.parser')

                # Find all links
                for a in soup.find_all('a', href=True):
                    href = a['href'].lower()
                    if any(ext in href for ext in ['.xls', '.xlsx', '.csv', '.zip']):
                        links.append({
                            'text': a.get_text(strip=True)[:50],
                            'href': a['href'],
                        })
            except ImportError:
                # Fallback to regex if BeautifulSoup not available
                import re
                pattern = r'href=["\']([^"\']*\.(xls|xlsx|csv|zip)[^"\']*)["\']'
                matches = re.findall(pattern, response.text, re.IGNORECASE)
                for match in matches:
                    links.append({
                        'text': 'Download link',
                        'href': match[0],
                    })
    except Exception as e:
        print(f"    Error searching for links: {e}")

    return links


def generate_report(results: dict) -> str:
    """Generate a validation report."""
    lines = [
        "=" * 70,
        "PRETOR (MITECO) DATA SOURCE VALIDATION REPORT",
        f"Generated: {datetime.now().isoformat()}",
        "=" * 70,
        "",
    ]

    # Portal accessibility
    lines.append("## Portal Accessibility")
    for name, result in results['urls'].items():
        status = "[OK]" if result['accessible'] else "[FAIL]"
        dl_status = " (has download links)" if result.get('has_download_links') else ""
        lines.append(f"  {status} {name}{dl_status}")
        lines.append(f"       URL: {result['url']}")
        if result['error']:
            lines.append(f"       Error: {result['error']}")
    lines.append("")

    # Download links found
    if results.get('download_links'):
        lines.append("## Download Links Found")
        for link in results['download_links'][:10]:
            lines.append(f"  - {link['text']}")
            lines.append(f"    {link['href']}")
        if len(results['download_links']) > 10:
            lines.append(f"  ... and {len(results['download_links']) - 10} more")
        lines.append("")

    # Parsing capabilities
    lines.append("## Parsing Capabilities")
    parsing = results.get('parsing', {})
    if parsing.get('pandas_available'):
        lines.append("  [OK] pandas available - can use pd.read_excel()")
    if parsing.get('can_parse_legacy_xls'):
        lines.append("  [OK] xlrd available - can parse legacy .xls files")
    if parsing.get('can_parse_xlsx'):
        lines.append("  [OK] openpyxl available - can parse .xlsx files")

    missing = []
    if not parsing.get('xlrd_available'):
        missing.append("xlrd (for legacy .xls)")
    if not parsing.get('openpyxl_available'):
        missing.append("openpyxl (for .xlsx)")
    if not parsing.get('pandas_available'):
        missing.append("pandas (recommended)")

    if missing:
        lines.append(f"\n  Missing libraries: {', '.join(missing)}")
        lines.append("  Install with: pip install " + " ".join(m.split()[0] for m in missing))
    lines.append("")

    # Recommendations
    lines.append("## Recommendations")

    portal_ok = any(r['accessible'] for r in results['urls'].values())
    has_downloads = any(r.get('has_download_links') for r in results['urls'].values())
    can_parse = parsing.get('can_parse_legacy_xls') or parsing.get('can_parse_xlsx')

    if has_downloads and can_parse:
        lines.append("  [RECOMMENDED] Download XLS files and parse with pandas")
        lines.append("  - Files are publicly accessible")
        lines.append("  - May need to download by province/region")
    elif portal_ok:
        lines.append("  [ALTERNATIVE] Portal accessible but no direct download links found")
        lines.append("  - Navigate manually to find download section")
        lines.append("  - Check for API or bulk data request options")
    else:
        lines.append("  [BLOCKED] Portal not accessible")
        lines.append("  - Try again later (may be temporary)")
        lines.append("  - Contact MITECO for data access")

    lines.append("")
    lines.append("## Expected Data Fields")
    for field in EXPECTED_FIELDS:
        lines.append(f"  - {field}")

    lines.append("")
    lines.append("## Next Steps")
    lines.append("  1. Navigate to MITECO portal and locate PRETOR download section")
    lines.append("  2. Download sample XLS file for one province")
    lines.append("  3. Verify field names and data format")
    lines.append("  4. Build parser based on actual file structure")

    lines.append("")
    lines.append("=" * 70)

    return "\n".join(lines)


def main():
    print("=" * 70)
    print("PRETOR (MITECO) Data Source Validation - Spain")
    print("=" * 70)
    print()

    results = {
        'urls': {},
        'download_links': [],
        'parsing': None,
    }

    # Test URL accessibility
    print("[1/3] Testing portal accessibility...")
    for name, url in PRETOR_URLS.items():
        results['urls'][name] = test_url_accessibility(name, url)
    print()

    # Search for download links
    print("[2/3] Searching for download links...")
    for name, url_result in results['urls'].items():
        if url_result['accessible']:
            links = search_for_download_links(url_result['url'])
            results['download_links'].extend(links)

    if results['download_links']:
        print(f"  Found {len(results['download_links'])} download links")
        for link in results['download_links'][:3]:
            print(f"    - {link['text'][:40]}...")
    else:
        print("  No download links found (may need manual navigation)")
    print()

    # Test parsing capabilities
    print("[3/3] Testing XLS parsing capabilities...")
    results['parsing'] = test_xls_parsing()
    print()

    # Generate report
    report = generate_report(results)
    print(report)

    # Save report
    report_path = 'data/pretor_validation_report.txt'
    try:
        with open(report_path, 'w') as f:
            f.write(report)
        print(f"\nReport saved to: {report_path}")
    except Exception as e:
        print(f"\nCould not save report: {e}")

    # Exit code based on results
    portal_ok = any(r['accessible'] for r in results['urls'].values())
    can_parse = results['parsing'].get('can_parse_legacy_xls') or results['parsing'].get('can_parse_xlsx')

    if portal_ok and can_parse:
        print("\n[SUCCESS] Portal accessible and parsing ready")
        sys.exit(0)
    elif portal_ok:
        print("\n[PARTIAL] Portal accessible but install parsing libraries")
        sys.exit(0)
    else:
        print("\n[BLOCKED] Portal not accessible")
        sys.exit(1)


if __name__ == '__main__':
    main()
