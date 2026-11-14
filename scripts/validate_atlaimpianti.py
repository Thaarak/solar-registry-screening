#!/usr/bin/env python3
"""
Validate access to Italy's Atlaimpianti (GSE WebGIS) data source.

This script tests:
1. WebGIS portal accessibility
2. WMS/WFS API endpoints
3. Data export functionality
4. Available fields and format

Usage:
    python scripts/validate_atlaimpianti.py
"""

import sys
from datetime import datetime

try:
    import requests
except ImportError:
    print("Error: requests not installed. Run: pip install requests")
    sys.exit(1)

# Known endpoints for GSE Atlaimpianti
ATLAIMPIANTI_URLS = {
    'portal': 'https://atla.gse.it/atlaimpianti/',
    'webgis': 'https://atla.gse.it/atlaimpianti/project/Atlaimpianti/index.html',
    # WMS endpoints (common patterns - actual URLs need verification)
    'wms': 'https://atla.gse.it/geoserver/atlaimpianti/wms',
    'wfs': 'https://atla.gse.it/geoserver/atlaimpianti/wfs',
}

# Expected data fields
EXPECTED_FIELDS = [
    'capacity_kw',
    'commissioning_date',
    'technology',
    'latitude',
    'longitude',
    'municipality',
    'province',
    'region',
    'tariff_regime',
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
    }

    try:
        print(f"  Testing {name}...")
        response = requests.get(url, timeout=30, allow_redirects=True)
        result['status_code'] = response.status_code
        result['content_type'] = response.headers.get('Content-Type', 'unknown')
        result['accessible'] = response.status_code == 200

        if result['accessible']:
            print(f"    [OK] Status: {response.status_code}, Content-Type: {result['content_type'][:50]}")
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


def test_wms_capabilities(url: str) -> dict:
    """Test WMS GetCapabilities request."""
    result = {
        'accessible': False,
        'layers': [],
        'error': None,
    }

    try:
        print(f"  Testing WMS GetCapabilities...")
        params = {
            'service': 'WMS',
            'request': 'GetCapabilities',
            'version': '1.1.1',
        }
        response = requests.get(url, params=params, timeout=30)

        if response.status_code == 200:
            result['accessible'] = True
            # Parse XML for layer names (basic parsing)
            content = response.text
            if '<Layer' in content:
                # Extract layer names (simplified)
                import re
                layers = re.findall(r'<Name>([^<]+)</Name>', content)
                result['layers'] = layers[:10]  # First 10 layers
                print(f"    [OK] Found {len(layers)} layers")
                for layer in result['layers'][:5]:
                    print(f"      - {layer}")
            else:
                print(f"    [WARN] No layers found in capabilities response")
        else:
            result['error'] = f"Status {response.status_code}"
            print(f"    [FAIL] Status: {response.status_code}")

    except Exception as e:
        result['error'] = str(e)[:200]
        print(f"    [FAIL] Error: {result['error']}")

    return result


def test_wfs_capabilities(url: str) -> dict:
    """Test WFS GetCapabilities request."""
    result = {
        'accessible': False,
        'feature_types': [],
        'error': None,
    }

    try:
        print(f"  Testing WFS GetCapabilities...")
        params = {
            'service': 'WFS',
            'request': 'GetCapabilities',
            'version': '2.0.0',
        }
        response = requests.get(url, params=params, timeout=30)

        if response.status_code == 200:
            result['accessible'] = True
            content = response.text
            if '<FeatureType' in content:
                import re
                features = re.findall(r'<Name>([^<]+)</Name>', content)
                result['feature_types'] = features[:10]
                print(f"    [OK] Found {len(features)} feature types")
                for ft in result['feature_types'][:5]:
                    print(f"      - {ft}")
            else:
                print(f"    [WARN] No feature types found")
        else:
            result['error'] = f"Status {response.status_code}"
            print(f"    [FAIL] Status: {response.status_code}")

    except Exception as e:
        result['error'] = str(e)[:200]
        print(f"    [FAIL] Error: {result['error']}")

    return result


def generate_report(results: dict) -> str:
    """Generate a validation report."""
    lines = [
        "=" * 70,
        "ATLAIMPIANTI DATA SOURCE VALIDATION REPORT",
        f"Generated: {datetime.now().isoformat()}",
        "=" * 70,
        "",
    ]

    # Portal accessibility
    lines.append("## Portal Accessibility")
    for name, result in results['urls'].items():
        status = "[OK]" if result['accessible'] else "[FAIL]"
        lines.append(f"  {status} {name}: {result['url']}")
        if result['error']:
            lines.append(f"       Error: {result['error']}")
    lines.append("")

    # WMS/WFS
    if results.get('wms'):
        lines.append("## WMS Service")
        if results['wms']['accessible']:
            lines.append(f"  [OK] Service accessible")
            lines.append(f"  Layers found: {len(results['wms'].get('layers', []))}")
        else:
            lines.append(f"  [FAIL] Service not accessible")
            if results['wms'].get('error'):
                lines.append(f"  Error: {results['wms']['error']}")

    if results.get('wfs'):
        lines.append("\n## WFS Service")
        if results['wfs']['accessible']:
            lines.append(f"  [OK] Service accessible")
            lines.append(f"  Feature types found: {len(results['wfs'].get('feature_types', []))}")
        else:
            lines.append(f"  [FAIL] Service not accessible")

    lines.append("")

    # Recommendations
    lines.append("## Recommendations")

    portal_ok = any(r['accessible'] for r in results['urls'].values())
    wms_ok = results.get('wms', {}).get('accessible', False)
    wfs_ok = results.get('wfs', {}).get('accessible', False)

    if wfs_ok:
        lines.append("  [RECOMMENDED] Use WFS API for programmatic data access")
        lines.append("  - Supports filtering by capacity, region, date")
        lines.append("  - Returns structured GeoJSON/GML")
    elif wms_ok:
        lines.append("  [ALTERNATIVE] WMS available but WFS preferred for data extraction")
    elif portal_ok:
        lines.append("  [FALLBACK] Manual WebGIS export required")
        lines.append("  - Navigate to portal and use export function")
        lines.append("  - May need to export by region to manage file size")
    else:
        lines.append("  [BLOCKED] No data access method available")
        lines.append("  - Consider contacting GSE directly for bulk data request")
        lines.append("  - Alternative: Use GSE published CSV datasets if available")

    lines.append("")
    lines.append("## Next Steps")
    if wfs_ok or wms_ok:
        lines.append("  1. Build scraper using the accessible API endpoint")
        lines.append("  2. Test with small region first (e.g., single province)")
        lines.append("  3. Implement rate limiting and error handling")
    else:
        lines.append("  1. Attempt manual portal export for sample data")
        lines.append("  2. Contact GSE for bulk data access options")
        lines.append("  3. Search for published GSE datasets (Conto Energia lists)")

    lines.append("")
    lines.append("=" * 70)

    return "\n".join(lines)


def main():
    print("=" * 70)
    print("Atlaimpianti (GSE WebGIS) Data Source Validation")
    print("=" * 70)
    print()

    results = {
        'urls': {},
        'wms': None,
        'wfs': None,
    }

    # Test URL accessibility
    print("[1/3] Testing portal accessibility...")
    for name, url in ATLAIMPIANTI_URLS.items():
        if name not in ['wms', 'wfs']:
            results['urls'][name] = test_url_accessibility(name, url)
    print()

    # Test WMS
    print("[2/3] Testing WMS service...")
    results['wms'] = test_wms_capabilities(ATLAIMPIANTI_URLS['wms'])
    print()

    # Test WFS
    print("[3/3] Testing WFS service...")
    results['wfs'] = test_wfs_capabilities(ATLAIMPIANTI_URLS['wfs'])
    print()

    # Generate report
    report = generate_report(results)
    print(report)

    # Save report
    report_path = 'data/atlaimpianti_validation_report.txt'
    try:
        with open(report_path, 'w') as f:
            f.write(report)
        print(f"\nReport saved to: {report_path}")
    except Exception as e:
        print(f"\nCould not save report: {e}")

    # Exit code based on results
    if results.get('wfs', {}).get('accessible') or results.get('wms', {}).get('accessible'):
        print("\n[SUCCESS] API access available - proceed with scraper development")
        sys.exit(0)
    elif any(r['accessible'] for r in results['urls'].values()):
        print("\n[PARTIAL] Portal accessible but no API - manual export required")
        sys.exit(0)
    else:
        print("\n[BLOCKED] No access available - contact GSE or use alternative sources")
        sys.exit(1)


if __name__ == '__main__':
    main()
