#!/usr/bin/env python3
"""
Analyze all call transcripts to extract insights about pests, patterns, and scope.
"""

import json
import os
import re
from collections import defaultdict, Counter
from pathlib import Path

# Pest keywords and variations
PEST_KEYWORDS = {
    'mice': r'\b(mouse|mice)\b',
    'rats': r'\b(rat|rats)\b',
    'ants': r'\b(ant|ants)\b',
    'cockroaches': r'\b(cockroach|cockroaches|roach|roaches)\b',
    'bed bugs': r'\b(bed bugs?|bedbugs?)\b',
    'wasps': r'\b(wasp|wasps|yellow jacket|yellow jackets)\b',
    'spiders': r'\b(spider|spiders)\b',
    'termites': r'\b(termite|termites)\b',
    'fleas': r'\b(flea|fleas)\b',
    'mosquitoes': r'\b(mosquito|mosquitoes|mosqu)\b',
    'flies': r'\b(fly|flies|fruit fly|fruit flies|gnats?)\b',
    'bees': r'\b(bee|bees|honey bee|carpenter bee)\b',
    'raccoons': r'\b(raccoon|raccoons)\b',
    'squirrels': r'\b(squirrel|squirrels)\b',
    'birds': r'\b(bird|birds|pigeon|seagull)\b',
    'beetles': r'\b(beetle|beetles|scarab|grub|grubs)\b',
    'silverfish': r'\b(silverfish)\b',
    'centipedes': r'\b(centipede|centipedes)\b',
    'mites': r'\b(mite|mites|dust mite)\b',
}

def extract_pests(text):
    """Extract pest types mentioned in a transcript."""
    pests = set()
    text_lower = text.lower()
    for pest_type, pattern in PEST_KEYWORDS.items():
        if re.search(pattern, text_lower, re.IGNORECASE):
            pests.add(pest_type)
    return pests

def extract_location(text):
    """Extract locations mentioned (attic, basement, kitchen, etc.)."""
    locations = set()
    location_keywords = {
        'attic': r'\b(attic|attics|loft)\b',
        'basement': r'\b(basement|basements)\b',
        'kitchen': r'\b(kitchen|kitchens)\b',
        'bathroom': r'\b(bathroom|bathrooms)\b',
        'bedroom': r'\b(bedroom|bedrooms)\b',
        'walls': r'\b(wall|walls|inside wall|void space)\b',
        'foundation': r'\b(foundation|crawlspace|crawl space)\b',
        'yard': r'\b(yard|yards|lawn|garden|outside|exterior)\b',
        'apartment': r'\b(apartment|apt|unit|condo|rental)\b',
        'garage': r'\b(garage|garages)\b',
    }
    text_lower = text.lower()
    for loc, pattern in location_keywords.items():
        if re.search(pattern, text_lower):
            locations.add(loc)
    return locations

def analyze_calls(transcripts_dir):
    """Analyze all transcripts and return insights."""
    pest_counts = Counter()
    location_counts = Counter()
    call_count = 0
    pest_location_pairs = defaultdict(Counter)

    # Collect all pests per call for frequency analysis
    pest_combinations = Counter()

    json_files = sorted(Path(transcripts_dir).glob('*.json'))

    for json_file in json_files:
        try:
            with open(json_file, 'r') as f:
                data = json.load(f)
                transcription = data.get('transcription', '')
                if not transcription:
                    continue

                pests = extract_pests(transcription)
                locations = extract_location(transcription)

                if pests:
                    call_count += 1
                    pest_counts.update(pests)
                    location_counts.update(locations)
                    pest_combinations[frozenset(pests)] += 1

                    for pest in pests:
                        for loc in locations:
                            pest_location_pairs[pest][loc] += 1
        except Exception as e:
            pass

    return {
        'total_calls_with_pests': call_count,
        'pest_frequency': pest_counts.most_common(20),
        'location_frequency': location_counts.most_common(15),
        'pest_combinations': pest_combinations.most_common(10),
        'pest_location_pairs': pest_location_pairs,
    }

if __name__ == '__main__':
    transcripts_dir = "/home/user/Projects/AI receptionist/all green 2025 transcripts"
    results = analyze_calls(transcripts_dir)

    print("=" * 70)
    print("CALL TRANSCRIPT ANALYSIS")
    print("=" * 70)
    print(f"\nTotal calls with pest mentions: {results['total_calls_with_pests']}")

    print("\n" + "=" * 70)
    print("TOP PESTS MENTIONED")
    print("=" * 70)
    for pest, count in results['pest_frequency']:
        print(f"  {pest:20} {count:4} calls ({100*count/results['total_calls_with_pests']:.1f}%)")

    print("\n" + "=" * 70)
    print("TOP LOCATIONS MENTIONED")
    print("=" * 70)
    for location, count in results['location_frequency']:
        print(f"  {location:20} {count:4} calls ({100*count/results['total_calls_with_pests']:.1f}%)")

    print("\n" + "=" * 70)
    print("MOST COMMON PEST COMBINATIONS (within same call)")
    print("=" * 70)
    for combo, count in results['pest_combinations'][:10]:
        pests = list(combo)
        print(f"  {' + '.join(pests):50} {count:3} calls")

    print("\n" + "=" * 70)
    print("TOP PEST + LOCATION PAIRS")
    print("=" * 70)
    all_pairs = []
    for pest, locations in results['pest_location_pairs'].items():
        for loc, count in locations.most_common(3):
            all_pairs.append((count, f"{pest} in {loc}"))
    all_pairs.sort(reverse=True)
    for count, pair in all_pairs[:20]:
        print(f"  {pair:40} {count:3} calls")
