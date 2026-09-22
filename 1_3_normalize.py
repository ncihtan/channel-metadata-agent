#!/usr/bin/env python3
"""
Step 1.3: Normalization

Normalizes raw antibody data files using column mappings from Schema Scout,
applies standardized column names, removes empty/technical rows,
and outputs a single consolidated parquet file.
"""

import os
import json
import argparse
import re
from pathlib import Path
import pandas as pd
import numpy as np
from typing import Dict, List, Optional


# Standard internal column names
STANDARD_COLUMNS = {
    'antibody_name': 'raw_target_name',
    'clone_id': 'clone',
    'channel_name': 'channel_name',
    'panel_id': 'panel_id',
    'lot_number': 'lot_number'
}

# Metadata columns to add
METADATA_COLUMNS = [
    'source_id',
    'source_file',
    'original_row_number'
]


def load_staging_manifest(manifest_path):
    """Load the staging manifest from Step 1.1."""
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    return manifest


def load_column_mapping_config(config_path):
    """Load the column mapping config from Step 1.2."""
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    return config


def is_technical_header(row, patterns=None):
    """
    Detect if a row is a technical header or separator line.

    Args:
        row: Pandas Series representing a row
        patterns: Optional list of regex patterns to match

    Returns:
        Boolean indicating if row should be dropped
    """
    if patterns is None:
        # Common patterns for technical headers
        patterns = [
            r'^-+$',  # Lines of dashes
            r'^=+$',  # Lines of equals
            r'^#+$',  # Lines of hashes
            r'^\*+$',  # Lines of asterisks
            r'^total:?',  # Total rows
            r'^summary:?',  # Summary rows
            r'^note:?',  # Note rows
            r'^antibody panel',  # Header repetitions
            r'^panel:',
            r'^metadata',
            r'^date:',
            r'^version:',
            r'^file:',
        ]

    # Convert row to string values
    str_values = row.astype(str).str.lower().str.strip()

    # Check if any cell matches a technical pattern
    for value in str_values:
        if pd.isna(value) or value == 'nan':
            continue
        for pattern in patterns:
            if re.match(pattern, value, re.IGNORECASE):
                return True

    return False


def is_empty_row(row):
    """
    Check if a row is effectively empty.

    Args:
        row: Pandas Series representing a row

    Returns:
        Boolean indicating if row is empty
    """
    # Count non-null, non-empty values
    non_empty = row.dropna()
    non_empty = non_empty[non_empty.astype(str).str.strip() != '']

    return len(non_empty) == 0


def normalize_file(file_path, file_type, file_name, source_id, column_mappings):
    """
    Normalize a single file using its column mappings.

    Args:
        file_path: Path to the file
        file_type: 'csv' or 'xlsx'
        file_name: Name of the file
        source_id: Unique identifier for the file
        column_mappings: Dictionary of column mappings for this file

    Returns:
        Normalized DataFrame or None if error
    """
    try:
        # Read the file
        if file_type == 'csv':
            df = pd.read_csv(file_path)
        elif file_type == 'xlsx':
            df = pd.read_excel(file_path)
        else:
            print(f"  ✗ Unsupported file type: {file_type}")
            return None

        if df.empty:
            print(f"  ⚠ File is empty")
            return None

        # Store original row numbers (before filtering)
        df['original_row_number'] = range(1, len(df) + 1)

        # Get mappings
        mappings = column_mappings.get('mappings', {})

        # Create new dataframe with standard columns
        normalized_df = pd.DataFrame()

        # Map columns to standard names
        for source_field, target_field in STANDARD_COLUMNS.items():
            source_column = mappings.get(source_field)

            if source_column and source_column in df.columns:
                normalized_df[target_field] = df[source_column]
            else:
                # Create empty column if mapping doesn't exist
                normalized_df[target_field] = None

        # Add metadata columns
        normalized_df['source_id'] = source_id
        normalized_df['source_file'] = file_name
        normalized_df['original_row_number'] = df['original_row_number']

        # Also include any unmapped columns as additional context
        unmapped_columns = column_mappings.get('unmapped_columns', [])
        for col in unmapped_columns:
            if col in df.columns:
                # Prefix with 'extra_' to indicate these are additional fields
                normalized_df[f'extra_{col}'] = df[col]

        initial_rows = len(normalized_df)

        # Remove empty rows
        empty_mask = normalized_df.apply(
            lambda row: is_empty_row(row[list(STANDARD_COLUMNS.values())]),
            axis=1
        )
        normalized_df = normalized_df[~empty_mask]
        empty_removed = initial_rows - len(normalized_df)

        # Remove technical header rows
        tech_mask = normalized_df.apply(
            lambda row: is_technical_header(row[list(STANDARD_COLUMNS.values())]),
            axis=1
        )
        normalized_df = normalized_df[~tech_mask]
        tech_removed = len(normalized_df) - (initial_rows - empty_removed)

        print(f"  ✓ Normalized: {len(normalized_df)} rows (removed {empty_removed} empty, {abs(tech_removed)} technical)")

        return normalized_df

    except Exception as e:
        print(f"  ✗ Error normalizing file: {e}")
        return None


def process_all_files(manifest, column_config, output_path='normalized_antibodies.parquet'):
    """
    Process all files and create a single normalized dataset.

    Args:
        manifest: Staging manifest from Step 1.1
        column_config: Column mapping config from Step 1.2
        output_path: Output parquet file path

    Returns:
        Combined DataFrame
    """
    all_dataframes = []
    files = manifest.get('files', [])
    file_mappings = column_config.get('files', {})

    print(f"Processing {len(files)} files...\n")

    for idx, file_info in enumerate(files, 1):
        file_path = file_info['file_path']
        file_name = file_info['file_name']
        file_type = file_info['file_type']
        source_id = file_info['source_id']

        print(f"[{idx}/{len(files)}] Normalizing: {file_name}")

        # Get column mappings for this file
        if file_name not in file_mappings:
            print(f"  ⚠ No column mappings found, skipping\n")
            continue

        column_mappings = file_mappings[file_name]

        # Normalize the file
        normalized_df = normalize_file(
            file_path,
            file_type,
            file_name,
            source_id,
            column_mappings
        )

        if normalized_df is not None and not normalized_df.empty:
            all_dataframes.append(normalized_df)

        print()

    if not all_dataframes:
        print("No data to normalize.")
        return None

    # Combine all dataframes
    print("Combining all normalized data...")
    combined_df = pd.concat(all_dataframes, ignore_index=True)

    return combined_df


def save_parquet(df, output_path='normalized_antibodies.parquet'):
    """
    Save the normalized dataframe to parquet format.

    Args:
        df: DataFrame to save
        output_path: Output file path
    """
    df.to_parquet(output_path, index=False, engine='pyarrow', compression='snappy')
    print(f"\nNormalized data saved to: {output_path}")


def print_summary(df):
    """Print a summary of the normalized dataset."""
    print("\n" + "=" * 60)
    print("NORMALIZATION SUMMARY")
    print("=" * 60)

    print(f"\nTotal rows: {len(df):,}")
    print(f"Total unique files: {df['source_file'].nunique()}")

    # Show coverage for each standard field
    print("\nField completeness:")
    for col in STANDARD_COLUMNS.values():
        if col in df.columns:
            non_null = df[col].notna().sum()
            percentage = (non_null / len(df) * 100) if len(df) > 0 else 0
            print(f"  {col:20s}: {non_null:7,}/{len(df):,} ({percentage:.1f}%)")

    # Show unique values for key fields
    print("\nUnique values:")
    for col in ['raw_target_name', 'clone', 'panel_id']:
        if col in df.columns:
            unique_count = df[col].nunique()
            print(f"  {col:20s}: {unique_count:,}")

    # Show file distribution
    print("\nRows per source file:")
    file_counts = df['source_file'].value_counts()
    for file_name, count in file_counts.head(10).items():
        print(f"  {file_name:40s}: {count:7,} rows")

    if len(file_counts) > 10:
        print(f"  ... and {len(file_counts) - 10} more files")

    # Data quality checks
    print("\nData quality:")
    completely_empty = df[list(STANDARD_COLUMNS.values())].isna().all(axis=1).sum()
    if completely_empty > 0:
        print(f"  ⚠ {completely_empty} rows have all standard fields empty")

    duplicates = df.duplicated(subset=['raw_target_name', 'clone', 'panel_id'], keep=False).sum()
    if duplicates > 0:
        print(f"  ⚠ {duplicates} potential duplicate rows (same target/clone/panel)")


def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description='Normalize antibody data files into a single standardized dataset'
    )
    parser.add_argument(
        '-m', '--manifest',
        default='/Users/ataylor/Downloads/ch_metadata_agent/htan_data/staging_manifest.json',
        help='Path to staging manifest from Step 1.1'
    )
    parser.add_argument(
        '-c', '--config',
        default='column_mapping_config.json',
        help='Path to column mapping config from Step 1.2'
    )
    parser.add_argument(
        '-o', '--output',
        default='normalized_antibodies.parquet',
        help='Output parquet file path (saved to current directory)'
    )

    args = parser.parse_args()

    try:
        # Load inputs
        print(f"Loading manifest: {args.manifest}")
        manifest = load_staging_manifest(args.manifest)

        print(f"Loading column mappings: {args.config}")
        column_config = load_column_mapping_config(args.config)

        print(f"Found {len(manifest.get('files', []))} files to process\n")

        # Process files
        combined_df = process_all_files(manifest, column_config, args.output)

        if combined_df is None or combined_df.empty:
            print("No data was normalized.")
            return 1

        # Save results
        save_parquet(combined_df, args.output)

        # Print summary
        print_summary(combined_df)

    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("Make sure the manifest and column mapping config files exist")
        print("Run Steps 1.1 and 1.2 first")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == '__main__':
    exit(main())
