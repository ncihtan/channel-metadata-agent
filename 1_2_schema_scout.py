#!/usr/bin/env python3
"""
Step 1.2: The Schema Scout (Agentic)

Uses Claude LLM to analyze file headers and identify relevant columns
for antibody metadata (Antibody Name, Clone ID, Channel Name, Panel ID, Lot Number).
"""

import os
import json
import argparse
from pathlib import Path
import pandas as pd
from anthropic import Anthropic
from typing import Dict, List, Any
import dotenv

# Load environment variables from .env file if present
dotenv.load_dotenv()


# Column types we're looking for
TARGET_COLUMNS = ["antibody_name", "clone_id", "channel_name", "panel_id", "lot_number"]


def load_staging_manifest(manifest_path="staging_manifest.json"):
    """Load the staging manifest from Step 1.1."""
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    return manifest


def read_file_sample(file_path, file_type, num_rows=5):
    """
    Read headers and first N rows from a CSV or XLSX file.

    Args:
        file_path: Path to the file
        file_type: 'csv' or 'xlsx'
        num_rows: Number of data rows to read (default: 5)

    Returns:
        Dictionary with 'headers' and 'sample_rows'
    """
    try:
        if file_type == "csv":
            df = pd.read_csv(file_path, nrows=num_rows)
        elif file_type == "xlsx":
            df = pd.read_excel(file_path, nrows=num_rows)
        else:
            raise ValueError(f"Unsupported file type: {file_type}")

        # Convert to dict format for easy serialization
        headers = df.columns.tolist()
        sample_rows = df.to_dict("records")

        return {
            "headers": headers,
            "sample_rows": sample_rows,
            "num_columns": len(headers),
            "num_sample_rows": len(sample_rows),
        }

    except Exception as e:
        print(f"Error reading file {file_path}: {e}")
        return None


def build_analysis_prompt(file_name, headers, sample_rows):
    """
    Build the prompt for Claude to analyze column mappings.

    Args:
        file_name: Name of the file being analyzed
        headers: List of column headers
        sample_rows: List of dictionaries containing sample data

    Returns:
        Prompt string
    """
    # Format sample data for display
    sample_data_str = json.dumps(sample_rows, indent=2)

    prompt = f"""Analyze the following data file headers and sample rows to identify columns related to antibody panel metadata.

File: {file_name}

Headers: {headers}

Sample Data (first {len(sample_rows)} rows):
{sample_data_str}

Your task is to identify which columns (if any) correspond to these standard fields:
1. **antibody_name**: The name or target of the antibody (e.g., "CD3", "CD45", "Ki67")
2. **clone_id**: The clone identifier (e.g., "UCHT1", "2D1")
3. **channel_name**: The fluorescence channel or marker name (e.g., "APC", "FITC", "Cy5")
4. **panel_id**: Panel name or identifier this antibody belongs to
5. **lot_number**: Lot or batch number of the antibody

**Instructions:**
- Match each target field to the most appropriate column in the data
- If a field cannot be confidently mapped, set its value to null
- If a column is ambiguous or could match multiple fields, flag it with a confidence level
- Consider column names AND the sample data values
- Look for variations in naming (e.g., "Ab_Name", "Antibody", "Target" for antibody_name)

Return your analysis as a JSON object with this exact structure:
{{
  "mappings": {{
    "antibody_name": "exact_column_name_or_null",
    "clone_id": "exact_column_name_or_null",
    "channel_name": "exact_column_name_or_null",
    "panel_id": "exact_column_name_or_null",
    "lot_number": "exact_column_name_or_null"
  }},
  "confidence": {{
    "antibody_name": "high|medium|low",
    "clone_id": "high|medium|low",
    "channel_name": "high|medium|low",
    "panel_id": "high|medium|low",
    "lot_number": "high|medium|low"
  }},
  "ambiguous_columns": [
    {{
      "column": "column_name",
      "possible_matches": ["field1", "field2"],
      "reason": "explanation"
    }}
  ],
  "unmapped_columns": ["list", "of", "columns", "not", "mapped"],
  "notes": "Any additional observations about the schema"
}}

Return ONLY the JSON object, no additional text."""

    return prompt


def analyze_with_claude(client, prompt, model="claude-sonnet-4-5-20250929"):
    """
    Send prompt to Claude and get analysis.

    Args:
        client: Anthropic client instance
        prompt: The analysis prompt
        model: Model to use

    Returns:
        Parsed JSON response
    """
    try:
        message = client.messages.create(
            model=model, max_tokens=2000, messages=[{"role": "user", "content": prompt}]
        )

        # Extract the response text
        response_text = message.content[0].text

        # Parse JSON response
        result = json.loads(response_text)
        return result

    except json.JSONDecodeError as e:
        print(f"Error parsing Claude response as JSON: {e}")
        print(f"Raw response: {response_text}")
        return None
    except Exception as e:
        print(f"Error calling Claude API: {e}")
        return None


def process_files(manifest, api_key, sample_rows=5, model="claude-sonnet-4-5-20250929"):
    """
    Process all files in the manifest and generate column mappings.

    Args:
        manifest: Staging manifest from Step 1.1
        api_key: Anthropic API key
        sample_rows: Number of sample rows to analyze
        model: Claude model to use

    Returns:
        Dictionary of column mappings per file
    """
    client = Anthropic(api_key=api_key)
    column_mappings = {}
    files = manifest.get("files", [])

    print(f"Processing {len(files)} files...\n")

    for idx, file_info in enumerate(files, 1):
        file_path = file_info["file_path"]
        file_name = file_info["file_name"]
        file_type = file_info["file_type"]
        source_id = file_info["source_id"]

        print(f"[{idx}/{len(files)}] Analyzing: {file_name}")

        # Read file sample
        sample_data = read_file_sample(file_path, file_type, sample_rows)

        if not sample_data:
            print(f"  ⚠ Skipping due to read error\n")
            continue

        # Build prompt
        prompt = build_analysis_prompt(
            file_name, sample_data["headers"], sample_data["sample_rows"]
        )

        # Analyze with Claude
        analysis = analyze_with_claude(client, prompt, model)

        if analysis:
            # Store results
            column_mappings[file_name] = {
                "source_id": source_id,
                "file_path": file_path,
                "mappings": analysis.get("mappings", {}),
                "confidence": analysis.get("confidence", {}),
                "ambiguous_columns": analysis.get("ambiguous_columns", []),
                "unmapped_columns": analysis.get("unmapped_columns", []),
                "notes": analysis.get("notes", ""),
                "total_columns": sample_data["num_columns"],
            }

            # Print summary
            mapped_count = sum(1 for v in analysis.get("mappings", {}).values() if v)
            print(f"  ✓ Mapped {mapped_count}/{len(TARGET_COLUMNS)} target columns")

            if analysis.get("ambiguous_columns"):
                print(
                    f"  ⚠ {len(analysis['ambiguous_columns'])} ambiguous columns flagged"
                )
        else:
            print(f"  ✗ Analysis failed\n")
            continue

        print()

    return column_mappings


def save_column_mapping_config(
    column_mappings, output_path="column_mapping_config.json"
):
    """
    Save the column mapping configuration to JSON.

    Args:
        column_mappings: Dictionary of mappings per file
        output_path: Output file path
    """
    config = {
        "generated_at": pd.Timestamp.now().isoformat(),
        "total_files": len(column_mappings),
        "files": column_mappings,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"Column mapping config saved to: {output_path}")


def print_summary(column_mappings):
    """Print a summary of the mapping results."""
    print("\n" + "=" * 60)
    print("SCHEMA SCOUT SUMMARY")
    print("=" * 60)

    total_files = len(column_mappings)
    print(f"\nTotal files analyzed: {total_files}")

    # Count how many files have each target column mapped
    field_coverage = {field: 0 for field in TARGET_COLUMNS}

    for file_data in column_mappings.values():
        mappings = file_data.get("mappings", {})
        for field in TARGET_COLUMNS:
            if mappings.get(field):
                field_coverage[field] += 1

    print("\nField coverage across all files:")
    for field, count in field_coverage.items():
        percentage = (count / total_files * 100) if total_files > 0 else 0
        print(f"  {field:20s}: {count:3d}/{total_files} files ({percentage:.1f}%)")

    # Count ambiguous cases
    total_ambiguous = sum(
        len(fd.get("ambiguous_columns", [])) for fd in column_mappings.values()
    )

    if total_ambiguous > 0:
        print(f"\n⚠ Total ambiguous columns flagged: {total_ambiguous}")


def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description="Analyze file schemas using Claude LLM to map columns"
    )
    parser.add_argument(
        "-m",
        "--manifest",
        default="staging_manifest.json",
        help="Path to staging manifest from Step 1.1",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="column_mapping_config.json",
        help="Output column mapping config file (saved to current directory)",
    )
    parser.add_argument(
        "-k", "--api-key", help="Anthropic API key (or set ANTHROPIC_API_KEY env var)"
    )
    parser.add_argument(
        "-n",
        "--num-rows",
        type=int,
        default=5,
        help="Number of sample rows to analyze (default: 5)",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-5-20250929",
        help="Claude model to use (default: claude-sonnet-4-5-20250929)",
    )

    args = parser.parse_args()

    # Get API key
    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: Anthropic API key required.")
        print("Provide via --api-key flag or ANTHROPIC_API_KEY environment variable.")
        return 1

    try:
        # Load staging manifest
        print(f"Loading manifest: {args.manifest}")
        manifest = load_staging_manifest(args.manifest)
        print(f"Found {len(manifest.get('files', []))} files in manifest\n")

        # Process files
        column_mappings = process_files(
            manifest, api_key, sample_rows=args.num_rows, model=args.model
        )

        if not column_mappings:
            print("No files were successfully processed.")
            return 1

        # Save results
        save_column_mapping_config(column_mappings, args.output)

        # Print summary
        print_summary(column_mappings)

    except FileNotFoundError as e:
        print(f"Error: {e}")
        print(f"Make sure {args.manifest} exists (run Step 1.1 first)")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
