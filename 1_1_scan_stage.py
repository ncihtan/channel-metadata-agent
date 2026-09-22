#!/usr/bin/env python3
"""
Step 1.1: File Scanning & Staging

This script recursively scans a directory for CSV and XLSX files,
generates unique source IDs for each file, and outputs a staging manifest.
"""

import os
import json
import hashlib
from pathlib import Path
from datetime import datetime
import argparse


def get_file_creation_time(file_path):
    """Get the creation time of a file."""
    stat = os.stat(file_path)
    # Use birth time if available (macOS), otherwise use modification time
    if hasattr(stat, 'st_birthtime'):
        return stat.st_birthtime
    else:
        return stat.st_mtime


def generate_source_id(file_path):
    """
    Generate a unique source_id based on filename and creation date.

    Args:
        file_path: Path to the file

    Returns:
        A hexadecimal hash string
    """
    filename = os.path.basename(file_path)
    creation_time = get_file_creation_time(file_path)

    # Create a string combining filename and creation timestamp
    unique_string = f"{filename}_{creation_time}"

    # Generate SHA256 hash
    hash_object = hashlib.sha256(unique_string.encode())
    source_id = hash_object.hexdigest()

    return source_id


def get_file_type(file_path):
    """
    Determine if a file is CSV or XLSX.

    Args:
        file_path: Path to the file

    Returns:
        'csv', 'xlsx', or None
    """
    extension = Path(file_path).suffix.lower()

    if extension == '.csv':
        return 'csv'
    elif extension in ['.xlsx', '.xls']:
        return 'xlsx'
    else:
        return None


def scan_directory(directory_path):
    """
    Recursively scan directory for CSV and XLSX files.

    Args:
        directory_path: Root directory to scan

    Returns:
        List of dictionaries containing file information
    """
    files_data = []

    # Convert to Path object
    root_path = Path(directory_path)

    if not root_path.exists():
        raise ValueError(f"Directory does not exist: {directory_path}")

    if not root_path.is_dir():
        raise ValueError(f"Path is not a directory: {directory_path}")

    # Recursively find all files
    for file_path in root_path.rglob('*'):
        if file_path.is_file():
            file_type = get_file_type(file_path)

            # Only process CSV and XLSX files
            if file_type:
                source_id = generate_source_id(file_path)

                file_info = {
                    'source_id': source_id,
                    'file_path': str(file_path.absolute()),
                    'file_name': file_path.name,
                    'file_type': file_type,
                    'relative_path': str(file_path.relative_to(root_path)),
                    'file_size_bytes': file_path.stat().st_size,
                    'scanned_at': datetime.now().isoformat()
                }

                files_data.append(file_info)

    return files_data


def save_manifest(files_data, output_path='staging_manifest.json'):
    """
    Save the staging manifest to a JSON file.

    Args:
        files_data: List of file information dictionaries
        output_path: Path to output JSON file
    """
    manifest = {
        'scan_timestamp': datetime.now().isoformat(),
        'total_files': len(files_data),
        'files': files_data
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"Staging manifest saved to: {output_path}")
    print(f"Total files processed: {len(files_data)}")


def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description='Scan directory for CSV/XLSX files and generate staging manifest'
    )
    parser.add_argument(
        'directory',
        nargs='?',
        default='/Users/ataylor/Downloads/ch_metadata_agent/htan_data',
        help='Directory path to scan (default: /Users/ataylor/Downloads/ch_metadata_agent/htan_data)'
    )
    parser.add_argument(
        '-o', '--output',
        default='staging_manifest.json',
        help='Output manifest file path (saved to current directory, default: staging_manifest.json)'
    )

    args = parser.parse_args()

    try:
        print(f"Scanning directory: {args.directory}")
        print("-" * 60)

        files_data = scan_directory(args.directory)

        if not files_data:
            print("No CSV or XLSX files found.")
            return

        # Print summary
        csv_count = sum(1 for f in files_data if f['file_type'] == 'csv')
        xlsx_count = sum(1 for f in files_data if f['file_type'] == 'xlsx')

        print(f"\nFound {len(files_data)} files:")
        print(f"  - CSV files: {csv_count}")
        print(f"  - XLSX files: {xlsx_count}")
        print()

        save_manifest(files_data, args.output)

    except Exception as e:
        print(f"Error: {e}")
        return 1

    return 0


if __name__ == '__main__':
    exit(main())
