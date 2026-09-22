#!/usr/bin/env python3
"""
Step 2.2: The Taxonomy Steward (Agentic Classification)

Uses Claude LLM to classify antibody targets into categories:
- protein: Individual protein targets (CD3, CD8a, Ki67)
- phospho_protein: Phosphorylated protein forms (pS6, p-ERK)
- chemical_stain: Chemical stains (DAPI, Hoechst)
- biological_group: Broad biological categories (Pan-CK, Collagen)
- structural: Structural/cytoskeletal markers (Actin, Tubulin)
- unknown: Cannot confidently classify
"""

import os
import json
import argparse
from typing import List, Dict, Optional
import pandas as pd
from anthropic import Anthropic


# Classification categories
CLASSIFICATION_CATEGORIES = [
    'protein',
    'phospho_protein',
    'chemical_stain',
    'biological_group',
    'structural',
    'unknown'
]


def load_term_clusters(clusters_path: str) -> Dict:
    """Load term clusters from Step 2.1."""
    with open(clusters_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data


def build_classification_prompt(terms: List[Dict], batch_size: int = 50) -> str:
    """
    Build prompt for Claude to classify antibody targets.

    Args:
        terms: List of term cluster dictionaries
        batch_size: Number of terms in this batch

    Returns:
        Prompt string
    """
    # Prepare term list for the prompt
    terms_list = []
    for idx, term in enumerate(terms[:batch_size], 1):
        representative = term['representative']
        variants = term['variants'][:3]  # Show up to 3 variants
        terms_list.append(f"{idx}. {representative} (variants: {', '.join(variants)})")

    terms_text = '\n'.join(terms_list)

    prompt = f"""You are a biomedical expert specializing in flow cytometry, immunohistochemistry, and antibody panels. Your task is to classify antibody target names into standardized categories.

**Terms to classify:**
{terms_text}

**Classification Categories:**

1. **protein**: Individual, specific protein targets
   - Examples: CD3, CD8a, Ki67, FOXP3, CD45
   - Includes cell surface markers, transcription factors, enzymes
   - Must refer to a single, specific protein

2. **phospho_protein**: Phosphorylated forms of proteins
   - Examples: pS6, p-ERK, phospho-Rb, p-STAT3
   - Look for: "p-", "phospho-", "pS", "pT", "pY" prefixes
   - Represents post-translational modification state

3. **chemical_stain**: Chemical dyes and non-antibody stains
   - Examples: DAPI, Hoechst, PI, 7-AAD, propidium iodide
   - Used for DNA/RNA staining, viability assessment
   - Not protein-based targets

4. **biological_group**: Broad categories or families of molecules
   - Examples: Pan-CK (pan-cytokeratin), IgG, collagen
   - Represents multiple related proteins
   - "Pan-" prefix is a strong indicator

5. **structural**: Cytoskeletal and structural components
   - Examples: actin, tubulin, vimentin, α-smooth muscle actin
   - Structural/cytoskeletal proteins
   - Often used for morphology or cell type identification

6. **unknown**: Cannot confidently classify
   - Use when the term is ambiguous, unclear, or doesn't fit categories
   - Use when you lack sufficient context
   - Better to mark as unknown than guess incorrectly

**Instructions:**
- Consider both the representative term AND its variants
- Look for common patterns and prefixes
- Use your knowledge of immunology and cell biology
- When uncertain between categories, prefer 'unknown'
- Phosphorylated forms should ALWAYS be 'phospho_protein', not 'protein'

**Output Format:**
Return a JSON array with exactly {len(terms[:batch_size])} objects, one for each term in order:

[
  {{
    "representative": "term_name",
    "classification": "category_name",
    "confidence": "high|medium|low",
    "reasoning": "Brief explanation (1 sentence)",
    "notes": "Optional additional context"
  }},
  ...
]

**Important:**
- Return ONLY the JSON array, no additional text
- Ensure the array has exactly {len(terms[:batch_size])} objects
- Match the order of the input terms exactly
- Use only the category names listed above

Return your classification now:"""

    return prompt


def classify_with_claude(
    client: Anthropic,
    terms: List[Dict],
    batch_size: int = 50,
    model: str = "claude-3-5-sonnet-20241022"
) -> List[Dict]:
    """
    Classify a batch of terms using Claude.

    Args:
        client: Anthropic client instance
        terms: List of term cluster dictionaries
        batch_size: Number of terms to classify per API call
        model: Claude model to use

    Returns:
        List of classification results
    """
    try:
        prompt = build_classification_prompt(terms, batch_size)

        message = client.messages.create(
            model=model,
            max_tokens=4000,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        # Extract response
        response_text = message.content[0].text

        # Parse JSON
        classifications = json.loads(response_text)

        # Validate
        if not isinstance(classifications, list):
            raise ValueError("Expected list of classifications")

        if len(classifications) != len(terms[:batch_size]):
            print(f"  ⚠ Warning: Expected {len(terms[:batch_size])} classifications, got {len(classifications)}")

        return classifications

    except json.JSONDecodeError as e:
        print(f"  ✗ Error parsing Claude response: {e}")
        print(f"  Raw response: {response_text[:500]}")
        return []
    except Exception as e:
        print(f"  ✗ Error calling Claude API: {e}")
        return []


def process_terms_in_batches(
    clusters: List[Dict],
    api_key: str,
    batch_size: int = 50,
    model: str = "claude-3-5-sonnet-20241022"
) -> List[Dict]:
    """
    Process all term clusters in batches.

    Args:
        clusters: List of term clusters
        api_key: Anthropic API key
        batch_size: Terms per batch
        model: Claude model to use

    Returns:
        List of classified terms
    """
    client = Anthropic(api_key=api_key)
    all_classifications = []

    total_batches = (len(clusters) + batch_size - 1) // batch_size

    print(f"Classifying {len(clusters)} terms in {total_batches} batches...\n")

    for batch_idx in range(0, len(clusters), batch_size):
        batch_num = (batch_idx // batch_size) + 1
        batch = clusters[batch_idx:batch_idx + batch_size]

        print(f"[Batch {batch_num}/{total_batches}] Classifying {len(batch)} terms...")

        classifications = classify_with_claude(client, batch, len(batch), model)

        if classifications:
            # Merge with original cluster data
            for cluster, classification in zip(batch, classifications):
                merged = {
                    'representative': cluster['representative'],
                    'variants': cluster['variants'],
                    'variant_count': cluster['count'],
                    'classification': classification.get('classification', 'unknown'),
                    'confidence': classification.get('confidence', 'low'),
                    'reasoning': classification.get('reasoning', ''),
                    'notes': classification.get('notes', '')
                }
                all_classifications.append(merged)

            print(f"  ✓ Classified {len(classifications)} terms")
        else:
            print(f"  ✗ Batch failed, marking as unknown")
            # Mark failed batch as unknown
            for cluster in batch:
                all_classifications.append({
                    'representative': cluster['representative'],
                    'variants': cluster['variants'],
                    'variant_count': cluster['count'],
                    'classification': 'unknown',
                    'confidence': 'low',
                    'reasoning': 'Classification failed',
                    'notes': 'API error or parsing failure'
                })

        print()

    return all_classifications


def filter_for_next_stage(classifications: List[Dict]) -> Dict:
    """
    Separate terms by classification for downstream processing.

    Args:
        classifications: List of classified terms

    Returns:
        Dictionary with terms grouped by processing needs
    """
    # Terms that need UniProt lookup (proteins)
    needs_uniprot = [
        c for c in classifications
        if c['classification'] in ['protein', 'phospho_protein']
    ]

    # Terms that are resolved (non-proteins)
    resolved = [
        c for c in classifications
        if c['classification'] in ['chemical_stain', 'biological_group', 'structural']
    ]

    # Terms that need manual review
    needs_review = [
        c for c in classifications
        if c['classification'] == 'unknown'
    ]

    return {
        'needs_uniprot': needs_uniprot,
        'resolved': resolved,
        'needs_review': needs_review
    }


def save_classifications(
    classifications: List[Dict],
    stage_data: Dict,
    output_path: str = 'taxonomy_classifications.json'
):
    """
    Save classification results to JSON.

    Args:
        classifications: All classification results
        stage_data: Data grouped by processing stage
        output_path: Output file path
    """
    output = {
        'generated_at': pd.Timestamp.now().isoformat(),
        'total_terms': len(classifications),
        'classification_summary': {
            cat: sum(1 for c in classifications if c['classification'] == cat)
            for cat in CLASSIFICATION_CATEGORIES
        },
        'next_stage_summary': {
            'needs_uniprot_lookup': len(stage_data['needs_uniprot']),
            'resolved_non_proteins': len(stage_data['resolved']),
            'needs_manual_review': len(stage_data['needs_review'])
        },
        'all_classifications': classifications,
        'needs_uniprot': stage_data['needs_uniprot'],
        'resolved': stage_data['resolved'],
        'needs_review': stage_data['needs_review']
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Classifications saved to: {output_path}")


def print_summary(classifications: List[Dict], stage_data: Dict):
    """Print summary of classification results."""
    print("\n" + "=" * 60)
    print("TAXONOMY STEWARD SUMMARY")
    print("=" * 60)

    total = len(classifications)
    print(f"\nTotal terms classified: {total}")

    # Classification breakdown
    print("\nClassification breakdown:")
    for cat in CLASSIFICATION_CATEGORIES:
        count = sum(1 for c in classifications if c['classification'] == cat)
        percentage = (count / total * 100) if total > 0 else 0
        print(f"  {cat:20s}: {count:4d} ({percentage:5.1f}%)")

    # Confidence breakdown
    print("\nConfidence levels:")
    for conf in ['high', 'medium', 'low']:
        count = sum(1 for c in classifications if c['confidence'] == conf)
        percentage = (count / total * 100) if total > 0 else 0
        print(f"  {conf:20s}: {count:4d} ({percentage:5.1f}%)")

    # Next stage routing
    print("\nNext stage routing:")
    print(f"  → UniProt lookup:      {len(stage_data['needs_uniprot']):4d} terms (proteins)")
    print(f"  → Resolved:            {len(stage_data['resolved']):4d} terms (non-proteins)")
    print(f"  → Manual review:       {len(stage_data['needs_review']):4d} terms (unknown)")

    # Show examples from each category
    print("\nExample classifications:")
    for cat in CLASSIFICATION_CATEGORIES:
        examples = [c for c in classifications if c['classification'] == cat][:3]
        if examples:
            print(f"\n  {cat}:")
            for ex in examples:
                print(f"    • {ex['representative']:15s} - {ex['reasoning']}")


def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description='Classify antibody targets using Claude LLM'
    )
    parser.add_argument(
        '-i', '--input',
        default='unique_term_clusters.json',
        help='Path to term clusters from Step 2.1'
    )
    parser.add_argument(
        '-o', '--output',
        default='taxonomy_classifications.json',
        help='Output classifications JSON file'
    )
    parser.add_argument(
        '-k', '--api-key',
        help='Anthropic API key (or set ANTHROPIC_API_KEY env var)'
    )
    parser.add_argument(
        '-b', '--batch-size',
        type=int,
        default=50,
        help='Number of terms to classify per API call (default: 50)'
    )
    parser.add_argument(
        '--model',
        default='claude-3-5-sonnet-20241022',
        help='Claude model to use'
    )

    args = parser.parse_args()

    # Get API key
    api_key = args.api_key or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        print("Error: Anthropic API key required.")
        print("Provide via --api-key flag or ANTHROPIC_API_KEY environment variable.")
        return 1

    try:
        # Load term clusters
        print(f"Loading term clusters: {args.input}")
        clusters_data = load_term_clusters(args.input)
        clusters = clusters_data.get('clusters', [])

        if not clusters:
            print("No clusters found to classify.")
            return 1

        print(f"Found {len(clusters)} term clusters\n")

        # Classify all terms
        classifications = process_terms_in_batches(
            clusters,
            api_key,
            batch_size=args.batch_size,
            model=args.model
        )

        if not classifications:
            print("No classifications generated.")
            return 1

        # Filter for next stage
        stage_data = filter_for_next_stage(classifications)

        # Save results
        save_classifications(classifications, stage_data, args.output)

        # Print summary
        print_summary(classifications, stage_data)

    except FileNotFoundError as e:
        print(f"Error: {e}")
        print(f"Make sure {args.input} exists (run Step 2.1 first)")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == '__main__':
    exit(main())
