#!/usr/bin/env python3
"""
Step 2.3: The Bio-Resolver (Agentic)

Uses Claude with UniProt API integration to map antibody targets to canonical names
with rich metadata. Claude determines the canonical name following clinical conventions
and then retrieves biological metadata from UniProt.
"""

import os
import json
import argparse
from pathlib import Path
from typing import List, Dict, Optional
import pandas as pd
from anthropic import Anthropic

# Import the existing UniProt API
from uniprot_api import UniProtAPI, UniProtEntry


def load_taxonomy_classifications(classifications_path: str) -> Dict:
    """Load taxonomy classifications from Step 2.2."""
    with open(classifications_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data


def build_resolver_prompt(term: str, classification: str, variants: List[str]) -> str:
    """
    Build prompt for Claude to resolve antibody target to canonical name.

    Args:
        term: Representative term to resolve
        classification: Classification (protein, phospho_protein)
        variants: List of variant names

    Returns:
        Prompt string
    """
    prompt = f"""You are an expert immunopathologist and flow cytometry specialist. Your task is to determine the canonical antigen name for an antibody target.

**Target to resolve:**
- Representative term: {term}
- Classification: {classification}
- Variant forms seen: {', '.join(variants[:5])}

**Naming Rules (in priority order):**

1. **CD Nomenclature**: If this is a Cluster of Differentiation marker, use CD notation
   - Example: "cd8" → "CD8" (not CD8A or CD8B)
   - Example: "cd45" → "CD45" (not PTPRC)
   - Keep CD names simple unless isoform is critical

2. **Preserve Phosphorylation State**: For phosphoproteins, keep phospho- prefix
   - Example: "ps6" → "pS6" (not RPS6)
   - Example: "phospho-erk" → "p-ERK" (not MAPK1)
   - Format: "p-" or "pS/pT/pY" for phosphorylated residues

3. **Clinical/Common Names**: Use widely recognized clinical names over gene symbols
   - Example: "her2" → "HER2" (not ERBB2)
   - Example: "ki67" → "Ki-67" (not MKI67)
   - Example: "nkp46" → "NKp46" or "NCR1" (both acceptable)

4. **Gene Symbols**: Only if no CD/clinical name exists
   - Example: "foxp3" → "FOXP3"
   - Uppercase for human proteins

5. **Isoforms/Splice Variants**: Include only if commonly distinguished
   - Example: "cd8a" → "CD8α" or "CD8a" if alpha chain is specifically targeted
   - Usually just "CD8" is sufficient

**Your Task:**

1. Determine the **canonical display name** following the rules above
2. Identify the best search term for UniProt lookup (usually the gene symbol)
3. Provide a brief explanation of your reasoning

**Output Format:**

Return a JSON object with this structure:

{{
  "canonical_name": "The display name following the rules",
  "uniprot_search_query": "Best term to search UniProt (e.g., gene symbol)",
  "naming_rationale": "Brief explanation of why you chose this name",
  "phospho_state": true/false,
  "alternative_names": ["list", "of", "other", "acceptable", "names"]
}}

**Important:**
- Return ONLY the JSON object, no additional text
- The canonical_name should be what scientists will recognize
- The uniprot_search_query should be what will find the right protein in UniProt
- Do NOT just default to the gene symbol if a CD or clinical name exists

Return your analysis now:"""

    return prompt


def resolve_with_claude(
    client: Anthropic,
    term: str,
    classification: str,
    variants: List[str],
    model: str = "claude-3-5-sonnet-20241022"
) -> Optional[Dict]:
    """
    Use Claude to determine canonical name and UniProt search strategy.

    Args:
        client: Anthropic client
        term: Term to resolve
        classification: Classification type
        variants: Variant forms
        model: Claude model to use

    Returns:
        Resolution dictionary or None if failed
    """
    try:
        prompt = build_resolver_prompt(term, classification, variants)

        message = client.messages.create(
            model=model,
            max_tokens=1000,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        # Extract and parse response
        response_text = message.content[0].text
        resolution = json.loads(response_text)

        return resolution

    except json.JSONDecodeError as e:
        print(f"  ✗ Error parsing Claude response: {e}")
        return None
    except Exception as e:
        print(f"  ✗ Error calling Claude: {e}")
        return None


def query_uniprot(
    uniprot_api: UniProtAPI,
    search_query: str,
    organism: str = "human"
) -> Optional[UniProtEntry]:
    """
    Query UniProt API for protein metadata.

    Args:
        uniprot_api: UniProtAPI instance
        search_query: Search term
        organism: Organism filter

    Returns:
        Best matching UniProtEntry or None
    """
    try:
        results = uniprot_api.search_protein(search_query, organism, reviewed=True)

        if results:
            # Return highest confidence result
            results.sort(key=lambda x: x.confidence_score, reverse=True)
            return results[0]

        return None

    except Exception as e:
        print(f"  ✗ UniProt API error: {e}")
        return None


def create_library_entry(
    term: str,
    classification: str,
    variants: List[str],
    resolution: Dict,
    uniprot_entry: Optional[UniProtEntry]
) -> Dict:
    """
    Create a library definition entry.

    Args:
        term: Original representative term
        classification: Classification type
        variants: Variant forms
        resolution: Claude's resolution
        uniprot_entry: UniProt metadata

    Returns:
        Library entry dictionary
    """
    entry = {
        'canonical_name': resolution.get('canonical_name', term),
        'display_name': resolution.get('canonical_name', term),
        'type': classification,
        'original_term': term,
        'variants': variants,
        'naming_rationale': resolution.get('naming_rationale', ''),
        'alternative_names': resolution.get('alternative_names', []),
        'phospho_state': resolution.get('phospho_state', False)
    }

    # Add UniProt metadata if available
    if uniprot_entry:
        entry.update({
            'uniprot_accession': uniprot_entry.accession,
            'gene_symbol': uniprot_entry.gene_name,
            'protein_name': uniprot_entry.protein_name,
            'organism': uniprot_entry.organism,
            'subcellular_location': uniprot_entry.subcellular_location,
            'function': uniprot_entry.function,
            'uniprot_confidence': uniprot_entry.confidence_score
        })
    else:
        entry.update({
            'uniprot_accession': None,
            'gene_symbol': None,
            'protein_name': None,
            'organism': None,
            'subcellular_location': [],
            'function': None,
            'uniprot_confidence': 0.0,
            'uniprot_lookup_failed': True
        })

    return entry


def process_terms(
    terms: List[Dict],
    anthropic_api_key: str,
    uniprot_cache_dir: Path = Path("data/cache"),
    model: str = "claude-3-5-sonnet-20241022"
) -> List[Dict]:
    """
    Process all terms requiring UniProt lookup.

    Args:
        terms: List of term dictionaries from taxonomy classification
        anthropic_api_key: Anthropic API key
        uniprot_cache_dir: UniProt cache directory
        model: Claude model to use

    Returns:
        List of library entries
    """
    claude_client = Anthropic(api_key=anthropic_api_key)
    uniprot_api = UniProtAPI(cache_dir=uniprot_cache_dir)

    library_entries = []

    print(f"Resolving {len(terms)} terms...\n")

    for idx, term_data in enumerate(terms, 1):
        term = term_data['representative']
        classification = term_data['classification']
        variants = term_data['variants']

        print(f"[{idx}/{len(terms)}] Resolving: {term}")

        # Step 1: Claude determines canonical name
        resolution = resolve_with_claude(
            claude_client,
            term,
            classification,
            variants,
            model
        )

        if not resolution:
            print(f"  ✗ Failed to resolve name\n")
            # Create minimal entry
            entry = {
                'canonical_name': term,
                'display_name': term,
                'type': classification,
                'original_term': term,
                'variants': variants,
                'resolution_failed': True
            }
            library_entries.append(entry)
            continue

        canonical_name = resolution.get('canonical_name', term)
        search_query = resolution.get('uniprot_search_query', canonical_name)

        print(f"  → Canonical: {canonical_name}")
        print(f"  → UniProt query: {search_query}")

        # Step 2: Query UniProt
        uniprot_entry = query_uniprot(uniprot_api, search_query)

        if uniprot_entry:
            print(f"  ✓ UniProt: {uniprot_entry.accession} ({uniprot_entry.gene_name})")
            print(f"    Confidence: {uniprot_entry.confidence_score:.2f}")
        else:
            print(f"  ⚠ No UniProt match found")

        # Step 3: Create library entry
        entry = create_library_entry(
            term,
            classification,
            variants,
            resolution,
            uniprot_entry
        )

        library_entries.append(entry)
        print()

    return library_entries


def save_library_definitions(
    entries: List[Dict],
    output_path: str = 'curated_library_definitions.json'
):
    """
    Save curated library definitions to JSON.

    Args:
        entries: List of library entry dictionaries
        output_path: Output file path
    """
    # Create dictionary keyed by canonical name
    library = {}
    for entry in entries:
        key = entry['canonical_name']
        library[key] = entry

    output = {
        'generated_at': pd.Timestamp.now().isoformat(),
        'total_entries': len(entries),
        'library': library
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Library definitions saved to: {output_path}")


def print_summary(entries: List[Dict]):
    """Print summary of resolution results."""
    print("\n" + "=" * 60)
    print("BIO-RESOLVER SUMMARY")
    print("=" * 60)

    total = len(entries)
    print(f"\nTotal terms resolved: {total}")

    # UniProt success rate
    with_uniprot = sum(1 for e in entries if e.get('uniprot_accession'))
    without_uniprot = total - with_uniprot

    print(f"\nUniProt lookup results:")
    print(f"  ✓ Successful: {with_uniprot} ({with_uniprot/total*100:.1f}%)")
    print(f"  ✗ Failed:     {without_uniprot} ({without_uniprot/total*100:.1f}%)")

    # Phospho proteins
    phospho_count = sum(1 for e in entries if e.get('phospho_state'))
    print(f"\nPhospho-proteins: {phospho_count}")

    # Classification breakdown
    types = {}
    for entry in entries:
        t = entry.get('type', 'unknown')
        types[t] = types.get(t, 0) + 1

    print(f"\nType breakdown:")
    for type_name, count in sorted(types.items()):
        print(f"  {type_name:20s}: {count}")

    # Show examples
    print(f"\nExample resolutions:")
    shown = 0
    for entry in entries:
        if shown >= 5:
            break

        original = entry.get('original_term')
        canonical = entry.get('canonical_name')
        gene = entry.get('gene_symbol')

        if original != canonical:
            info = f"  {original} → {canonical}"
            if gene and gene.upper() != canonical.upper():
                info += f" (gene: {gene})"
            print(info)
            shown += 1

    # High confidence matches
    high_conf = [e for e in entries if e.get('uniprot_confidence', 0) >= 0.9]
    print(f"\nHigh confidence UniProt matches (≥0.9): {len(high_conf)}")


def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description='Resolve antibody targets to canonical names with UniProt metadata'
    )
    parser.add_argument(
        '-i', '--input',
        default='taxonomy_classifications.json',
        help='Path to taxonomy classifications from Step 2.2'
    )
    parser.add_argument(
        '-o', '--output',
        default='curated_library_definitions.json',
        help='Output library definitions JSON file'
    )
    parser.add_argument(
        '-k', '--api-key',
        help='Anthropic API key (or set ANTHROPIC_API_KEY env var)'
    )
    parser.add_argument(
        '--cache-dir',
        default='data/cache',
        help='UniProt cache directory (default: data/cache)'
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
        # Load taxonomy classifications
        print(f"Loading classifications: {args.input}")
        classifications = load_taxonomy_classifications(args.input)

        # Get terms that need UniProt lookup
        needs_uniprot = classifications.get('needs_uniprot', [])

        if not needs_uniprot:
            print("No terms require UniProt lookup.")
            return 0

        print(f"Found {len(needs_uniprot)} terms requiring resolution\n")

        # Process all terms
        library_entries = process_terms(
            needs_uniprot,
            api_key,
            Path(args.cache_dir),
            args.model
        )

        if not library_entries:
            print("No library entries created.")
            return 1

        # Save results
        save_library_definitions(library_entries, args.output)

        # Print summary
        print_summary(library_entries)

    except FileNotFoundError as e:
        print(f"Error: {e}")
        print(f"Make sure {args.input} exists (run Step 2.2 first)")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == '__main__':
    exit(main())
