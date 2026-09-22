#!/usr/bin/env python3
"""
Step 2.1: The Scrub & Group (Phase 2: Antibody Harmonization)

Scrubs raw antibody names and clusters similar variants using fuzzy matching.
Prepares cleaned terms for canonical name mapping.
"""

import os
import json
import argparse
import re
from pathlib import Path
from typing import List, Dict, Set, Tuple
import pandas as pd
from collections import defaultdict
from difflib import SequenceMatcher


def scrub_antibody_name(name: str) -> str:
    """
    Clean and normalize an antibody name.

    Args:
        name: Raw antibody name

    Returns:
        Scrubbed antibody name
    """
    if pd.isna(name):
        return ""

    # Convert to string and lowercase
    scrubbed = str(name).lower().strip()

    # Strip "anti-" prefix (with various separators)
    scrubbed = re.sub(r'^anti[-\s_]*', '', scrubbed)

    # Strip common fluorophore suffixes
    fluorophores = [
        'fitc', 'pe', 'apc', 'percp', 'pacific blue', 'alexa',
        'cy3', 'cy5', 'cy7', 'bv421', 'bv510', 'bv605', 'bv711',
        'texas red', 'rhodamine', 'tritc', 'dapi', 'hoechst',
        'ef450', 'ef660', 'pe-cy7', 'apc-cy7', 'pe-texas red',
        'brilliant violet', 'bv', 'vioblue', 'viobright'
    ]

    # Build regex pattern for fluorophore removal
    fluorophore_pattern = r'[-_\s]*\(?(' + '|'.join(fluorophores) + r')\)?[-_\s]*\d*'
    scrubbed = re.sub(fluorophore_pattern, '', scrubbed, flags=re.IGNORECASE)

    # Strip parentheses and their contents
    scrubbed = re.sub(r'\([^)]*\)', '', scrubbed)

    # Remove common separators and extra spaces
    scrubbed = re.sub(r'[-_/\\]+', '', scrubbed)
    scrubbed = re.sub(r'\s+', '', scrubbed)

    # Remove trailing numbers that might be catalog numbers
    # but preserve important numbers like CD8, CD45
    # This is tricky - we'll keep numbers that are part of the name
    # scrubbed = re.sub(r'(?<=[a-z])\d{4,}$', '', scrubbed)

    return scrubbed.strip()


def calculate_levenshtein_distance(s1: str, s2: str) -> int:
    """
    Calculate Levenshtein distance between two strings.

    Args:
        s1: First string
        s2: Second string

    Returns:
        Edit distance
    """
    if len(s1) < len(s2):
        return calculate_levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            # Cost of insertions, deletions, or substitutions
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def similarity_ratio(s1: str, s2: str) -> float:
    """
    Calculate similarity ratio between two strings.

    Args:
        s1: First string
        s2: Second string

    Returns:
        Similarity ratio (0-1)
    """
    if not s1 or not s2:
        return 0.0

    # Use SequenceMatcher for similarity
    return SequenceMatcher(None, s1, s2).ratio()


def cluster_similar_terms(terms: List[Tuple[str, str]], threshold: float = 0.85) -> List[Dict]:
    """
    Cluster similar terms using fuzzy matching.

    Args:
        terms: List of tuples (original_name, scrubbed_name)
        threshold: Similarity threshold for clustering (0-1)

    Returns:
        List of cluster dictionaries
    """
    # Group by scrubbed name first (exact matches)
    exact_groups = defaultdict(set)
    for original, scrubbed in terms:
        if scrubbed:  # Skip empty scrubbed names
            exact_groups[scrubbed].add(original)

    # Now cluster the scrubbed names using fuzzy matching
    scrubbed_names = list(exact_groups.keys())
    clusters = []
    used = set()

    for i, name1 in enumerate(scrubbed_names):
        if name1 in used:
            continue

        # Start a new cluster
        cluster_variants = set(exact_groups[name1])
        cluster_scrubbed = {name1}
        used.add(name1)

        # Find similar names
        for j, name2 in enumerate(scrubbed_names):
            if i == j or name2 in used:
                continue

            # Calculate similarity
            sim = similarity_ratio(name1, name2)

            # Also check Levenshtein distance for short strings
            if len(name1) <= 5 or len(name2) <= 5:
                lev_dist = calculate_levenshtein_distance(name1, name2)
                max_len = max(len(name1), len(name2))
                lev_sim = 1 - (lev_dist / max_len) if max_len > 0 else 0
                sim = max(sim, lev_sim)

            if sim >= threshold:
                cluster_variants.update(exact_groups[name2])
                cluster_scrubbed.add(name2)
                used.add(name2)

        # Choose representative (shortest scrubbed name, or most common variant)
        representative = min(cluster_scrubbed, key=len)

        clusters.append({
            'representative': representative,
            'variants': sorted(list(cluster_variants)),
            'scrubbed_forms': sorted(list(cluster_scrubbed)),
            'count': len(cluster_variants)
        })

    # Sort clusters by count (most common first)
    clusters.sort(key=lambda x: x['count'], reverse=True)

    return clusters


def extract_unique_terms(parquet_path: str, column: str = 'raw_target_name') -> List[str]:
    """
    Extract unique terms from the normalized parquet file.

    Args:
        parquet_path: Path to the parquet file
        column: Column name to extract from

    Returns:
        List of unique terms
    """
    df = pd.read_parquet(parquet_path)

    if column not in df.columns:
        raise ValueError(f"Column '{column}' not found in parquet file")

    # Get unique non-null values
    unique_terms = df[column].dropna().unique().tolist()

    return unique_terms


def process_terms(unique_terms: List[str], threshold: float = 0.85) -> List[Dict]:
    """
    Process terms: scrub and cluster.

    Args:
        unique_terms: List of unique raw terms
        threshold: Clustering similarity threshold

    Returns:
        List of clusters
    """
    print(f"Processing {len(unique_terms)} unique terms...\n")

    # Scrub all terms
    print("Scrubbing terms...")
    term_pairs = []
    scrub_stats = {'empty': 0, 'unchanged': 0, 'changed': 0}

    for term in unique_terms:
        scrubbed = scrub_antibody_name(term)

        if not scrubbed:
            scrub_stats['empty'] += 1
            continue
        elif scrubbed.lower() == str(term).lower().strip():
            scrub_stats['unchanged'] += 1
        else:
            scrub_stats['changed'] += 1

        term_pairs.append((term, scrubbed))

    print(f"  Changed: {scrub_stats['changed']}")
    print(f"  Unchanged: {scrub_stats['unchanged']}")
    print(f"  Empty after scrubbing: {scrub_stats['empty']}")
    print()

    # Cluster similar terms
    print(f"Clustering with similarity threshold: {threshold}")
    clusters = cluster_similar_terms(term_pairs, threshold)

    print(f"  Created {len(clusters)} clusters")
    print()

    return clusters


def save_clusters(clusters: List[Dict], output_path: str = 'unique_term_clusters.json'):
    """
    Save clusters to JSON file.

    Args:
        clusters: List of cluster dictionaries
        output_path: Output file path
    """
    output = {
        'generated_at': pd.Timestamp.now().isoformat(),
        'total_clusters': len(clusters),
        'total_variants': sum(c['count'] for c in clusters),
        'clusters': clusters
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Clusters saved to: {output_path}")


def print_summary(clusters: List[Dict]):
    """Print summary of clustering results."""
    print("\n" + "=" * 60)
    print("SCRUB & GROUP SUMMARY")
    print("=" * 60)

    total_clusters = len(clusters)
    total_variants = sum(c['count'] for c in clusters)

    print(f"\nTotal clusters: {total_clusters}")
    print(f"Total variants: {total_variants}")
    print(f"Average variants per cluster: {total_variants / total_clusters:.1f}")

    # Show distribution
    singleton_count = sum(1 for c in clusters if c['count'] == 1)
    multi_variant = sum(1 for c in clusters if c['count'] > 1)

    print(f"\nCluster distribution:")
    print(f"  Single variant: {singleton_count}")
    print(f"  Multiple variants: {multi_variant}")

    # Show top clusters
    print(f"\nTop 10 clusters by variant count:")
    for i, cluster in enumerate(clusters[:10], 1):
        print(f"  {i}. {cluster['representative']:20s} ({cluster['count']} variants)")
        if cluster['count'] <= 5:
            for variant in cluster['variants'][:3]:
                print(f"      - {variant}")

    # Show examples of scrubbing
    print(f"\nExample scrubbing transformations:")
    examples_shown = 0
    for cluster in clusters:
        if cluster['count'] > 1 and examples_shown < 5:
            print(f"  {cluster['representative']}:")
            for variant in cluster['variants'][:3]:
                scrubbed = scrub_antibody_name(variant)
                if variant != scrubbed:
                    print(f"    {variant} → {scrubbed}")
                    examples_shown += 1
                    if examples_shown >= 5:
                        break


def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description='Scrub and cluster antibody names for harmonization'
    )
    parser.add_argument(
        '-i', '--input',
        default='normalized_antibodies.parquet',
        help='Path to normalized antibodies parquet file from Step 1.3'
    )
    parser.add_argument(
        '-o', '--output',
        default='unique_term_clusters.json',
        help='Output clusters JSON file (saved to current directory)'
    )
    parser.add_argument(
        '-c', '--column',
        default='raw_target_name',
        help='Column name to extract terms from (default: raw_target_name)'
    )
    parser.add_argument(
        '-t', '--threshold',
        type=float,
        default=0.85,
        help='Similarity threshold for clustering (0-1, default: 0.85)'
    )

    args = parser.parse_args()

    try:
        # Extract unique terms
        print(f"Loading data from: {args.input}")
        unique_terms = extract_unique_terms(args.input, args.column)

        # Process and cluster
        clusters = process_terms(unique_terms, args.threshold)

        if not clusters:
            print("No clusters created.")
            return 1

        # Save results
        save_clusters(clusters, args.output)

        # Print summary
        print_summary(clusters)

    except FileNotFoundError as e:
        print(f"Error: {e}")
        print(f"Make sure {args.input} exists (run Step 1.3 first)")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == '__main__':
    exit(main())
