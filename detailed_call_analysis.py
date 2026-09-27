#!/usr/bin/env python3
"""
Extract service types, pricing, and handling patterns from calls.
"""

import json
from pathlib import Path
import re
from collections import Counter

SERVICE_KEYWORDS = {
    'sealing': r'seal|seal.?up|caulk|gap',
    'one-way door': r'one.?way.?door|exclud',
    'trapping': r'trap|trapping',
    'treatment': r'treat|spray|poison|baiting',
    'inspection': r'inspect|assess|evaluate',
    'interior seal': r'interior.?seal|dishwasher|void space',
    'pricing talk': r'price|cost|fee|\$|plus.?gst',
    'lawn treatment': r'lawn|grub|scarab|landscape',
    'follow-up': r'follow.?up|callback|reschedule',
    'landlord issue': r'landlord|tenant|rental|lease',
}

def extract_services(text):
    """Extract service types mentioned."""
    services = set()
    text_lower = text.lower()
    for service, pattern in SERVICE_KEYWORDS.items():
        if re.search(pattern, text_lower):
            services.add(service)
    return services

def extract_prices(text):
    """Extract pricing mentions."""
    prices = re.findall(r'\$\s*(\d+)', text, re.IGNORECASE)
    return [int(p) for p in prices] if prices else []

def analyze_services(transcripts_dir, limit=1000):
    """Analyze service patterns across calls."""
    service_freq = Counter()
    service_combo = Counter()
    price_samples = []
    apartment_calls = 0
    outdoor_calls = 0

    json_files = sorted(Path(transcripts_dir).glob('*.json'))[:limit]

    for json_file in json_files:
        try:
            with open(json_file, 'r') as f:
                data = json.load(f)
                trans = data.get('transcription', '').lower()
                if not trans:
                    continue

                services = extract_services(trans)
                if services:
                    service_freq.update(services)
                    service_combo[frozenset(services)] += 1

                prices = extract_prices(trans)
                if prices and len(prices) <= 3:  # Ignore junk
                    price_samples.extend(prices)

                if 'apartment' in trans or 'tenant' in trans or 'landlord' in trans:
                    apartment_calls += 1

                if 'yard' in trans or 'lawn' in trans or 'roof' in trans:
                    outdoor_calls += 1

        except Exception as e:
            pass

    return {
        'service_freq': service_freq.most_common(15),
        'service_combos': service_combo.most_common(10),
        'price_samples': price_samples,
        'apartment_calls': apartment_calls,
        'outdoor_calls': outdoor_calls,
    }

if __name__ == '__main__':
    transcripts_dir = "/home/user/Projects/AI receptionist/all green 2025 transcripts"
    results = analyze_services(transcripts_dir, limit=1000)

    print("=" * 70)
    print("SERVICE TYPE FREQUENCY")
    print("=" * 70)
    for service, count in results['service_freq']:
        print(f"  {service:20} {count:4} calls")

    print("\n" + "=" * 70)
    print("MOST COMMON SERVICE COMBINATIONS")
    print("=" * 70)
    for combo, count in results['service_combos'][:8]:
        services = list(combo)
        print(f"  {' + '.join(services[:3]):50} {count:3} calls")

    print("\n" + "=" * 70)
    print("PRICE STATISTICS")
    print("=" * 70)
    if results['price_samples']:
        prices = sorted(results['price_samples'])
        print(f"  Samples: {len(prices)}")
        print(f"  Min: ${prices[0]}")
        print(f"  Max: ${prices[-1]}")
        print(f"  Median: ${prices[len(prices)//2]}")
        print(f"  Common prices: {Counter(prices).most_common(5)}")

    print("\n" + "=" * 70)
    print("CALL TYPE BREAKDOWN")
    print("=" * 70)
    print(f"  Apartment/rental issues: {results['apartment_calls']} calls")
    print(f"  Outdoor/roof issues: {results['outdoor_calls']} calls")
