# Intelligent Claude-based UniProt lookup with metadata context
from anthropic import Anthropic, transform_schema
from uniprot_api import lookup_protein
import json
import pandas as pd
import random
import re
import os
import json
from tqdm import tqdm
import hashlib
import dotenv

dotenv.load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

from pydantic import BaseModel, Field
from enum import Enum


class MarkerTypeEnum(str, Enum):
    chemical_stain = "chemical_stain"
    blank_or_background = "blank_or_background"
    protein_group = "protein_group"
    protein_single = "protein_single"
    chemical_element = "chemical_element"
    cd_marker = "cd_marker"
    other_dna = "other_dna"
    other = "other"


class ExtractedlMarkerSchema(BaseModel):
    extracted_marker: str = Field(
        ...,
        description="The most relevant canonical marker or identifier for the protein, stain or feature being imaged extracted from the metadata row content.",
    )
    marker_type: MarkerTypeEnum = Field(
        ..., description="The type of the canonical marker."
    )


# simple schema with uniprot id and subcellular location
#   where subcellular_location is one off cytoplasm, nucleus, cell memberane, extracellular.
class SubcellularLocationEnum(str, Enum):
    cytoplasm = "cytoplasm"
    nucleus = "nucleus"
    cell_membrane = "cell_membrane"
    extracellular = "extracellular"


class UniprotOutputSchema(BaseModel):
    accession: str = Field(..., description="The UniProt accession ID of the protein.")
    gene_name: str = Field(
        ..., description="The gene name associated with the protein."
    )
    protein_name: str = Field(..., description="The full name of the protein.")
    organism: str = Field(
        ..., description="The organism from which the protein is derived."
    )
    subcellular_location: SubcellularLocationEnum = Field(
        ..., description="List of subcellular locations where the protein is found."
    )
    # function: str = Field(..., description="A brief description of the protein's function.")
    confidence_score: float = Field(
        ..., description="Confidence score of the protein match (0 to 1)."
    )


# combine with the extracted marker schema
class MarkerUniprotSchema(ExtractedlMarkerSchema):
    uniprot_entry: UniprotOutputSchema | None = Field(
        None, description="The selected UniProt entry for the marker."
    )


# Define the tool - Claude calls this to search UniProt
tools = [
    {
        "name": "lookup_protein",
        "description": "Searches the UniProt database for protein information by gene name or protein name. Returns a list of matching protein entries with details like gene name, protein name, organism, subcellular location, function, and confidence score. Use this to find candidate proteins that match the marker.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The gene name or protein name to search for (e.g., 'CD20', 'FOXP3', 'Ki67')",
                },
                "organism": {
                    "type": "string",
                    "description": "The organism to filter by (default: 'human')",
                    "default": "human",
                },
            },
            "required": ["query"],
        },
    }
]


def curate_marker_with_claude(marker_entry):
    """
    Use Claude to intelligently curate a marker by:
    1. Looking up the marker in UniProt
    2. Using ALL the metadata context to select the best match
    3. Returning the selected UniProt entry with reasoning
    """

    extracted_marker = marker_entry["extracted_marker"]
    marker_type = marker_entry["marker_type"]
    row_contents = marker_entry["row_contents"]

    # Build a rich prompt with ALL context
    user_message = f"""I need you to find the correct UniProt entry for this marker used in imaging:

**Imaging Metadata** (use this context to select the right protein):
{json.dumps(row_contents, indent=2)}

Please:
1. Given the following imaging metadata row content, extract the most relevant canonical marker or identifier and classify its type. 
Return the result in JSON format with the fields 'extracted_marker' and 'marker_type'.

2. If the marker_type is 'protein_single', Search UniProt for the extracted marker
    a. Review all the results you find
    b. Use the imaging metadata (antibody clone, vendor, catalog number, target name, etc.) to determine which UniProt entry is the correct match
    c. If there are multiple good matches, explain why you chose one

    Return your analysis with:
    - The selected UniProt accession and gene name
    - Your confidence level (high/medium/low)
    - Your reasoning for the selection"""

    # Initial call - Claude decides to use the tool
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4096,
        tools=tools,
        messages=[{"role": "user", "content": user_message}],
    )

    # Check if Claude wants to use the tool
    if response.stop_reason == "tool_use":
        tool_use_block = next(
            block for block in response.content if block.type == "tool_use"
        )

        # Execute the UniProt lookup
        query = tool_use_block.input["query"]
        organism = tool_use_block.input.get("organism", "human")
        results = lookup_protein(query, organism)

        # Format results for Claude
        tool_result_text = f"Found {len(results)} UniProt entries for '{query}':\n\n"
        for i, entry in enumerate(results, 1):
            tool_result_text += f"Result {i}:\n"
            tool_result_text += f"  Accession: {entry.accession}\n"
            tool_result_text += f"  Gene: {entry.gene_name}\n"
            tool_result_text += f"  Protein: {entry.protein_name}\n"
            tool_result_text += f"  Organism: {entry.organism}\n"
            tool_result_text += f"  Confidence: {entry.confidence_score}\n"
            if entry.subcellular_location:
                tool_result_text += (
                    f"  Location: {', '.join(entry.subcellular_location)}\n"
                )
            if entry.function:
                tool_result_text += f"  Function: {entry.function[:300]}...\n"
            tool_result_text += "\n"

        # Continue conversation with tool results
        messages = [
            {"role": "user", "content": user_message},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_block.id,
                        "name": tool_use_block.name,
                        "input": tool_use_block.input,
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_block.id,
                        "content": tool_result_text,
                    }
                ],
            },
        ]

        # Get Claude's analysis
        final_response = client.beta.messages.create(
            model="claude-sonnet-4-5",
            betas=["structured-outputs-2025-11-13"],
            max_tokens=4096,
            tools=tools,
            messages=messages,
            output_format={
                "type": "json_schema",
                "schema": transform_schema(MarkerUniprotSchema),
            },
        )

        return final_response.content[0].text

    else:
        # Claude responded without using tools - still need structured output
        # Make another call with the original message to get structured response
        messages = [{"role": "user", "content": user_message}]

        final_response = client.beta.messages.create(
            model="claude-sonnet-4-5",
            betas=["structured-outputs-2025-11-13"],
            max_tokens=4096,
            messages=messages,
            output_format={
                "type": "json_schema",
                "schema": transform_schema(MarkerUniprotSchema),
            },
        )

        return final_response.content[0].text


# list files in /Users/ataylor/Downloads/ch_metadata_agent/htan_data


directory = "/Users/ataylor/Downloads/ch_metadata_agent/htan_data"
files = [f for f in os.listdir(directory)]
print(len(files))

# Based on the deduplication above write a function to parse each tsv or csv in the dict
# into json with the file hash as the key

# the json should be flat and therefore have
# row_hash, row_content as dict, and file_paths as list
# and be deduiplicated based on row content

# we can also drp these keys
# "Component": "ImagingLevel2", "Filename": "mxif_level_2/AFsubtracted/HTA11_3252_20000010115220260050000000000.tif", "File Format": "tif", "HTAN Participant ID": "HTA11_3252", "HTAN Parent Biospecimen ID": "HTA11_3252_2000001011", "HTAN Data File ID": "HTA11_3252_20000010115220260050000000000", "Channel Metadata Filename": "mxif_level_2/AFsubtracted/metadata/HTA11_3252_20000010115220260050000000000_metadata.csv", "Imaging Assay Type": "MxIF", "Protocol Link": "NONE", "Workflow Start Datetime": "09/29/2020", "Workflow End Datetime": "09/30/2020", "Software and Version": "ImageApp 1.0.0.0", "Microscope": "INCELL ANALYZER 2500 HS", "Objective": "NIKON MRD00205 Plan Apochromat", "NominalMagnification": "20X", "LensNA": 0.75, "WorkingDistance": 1, "WorkingDistanceUnit": "mm", "Immersion": "Air", "Pyramid": "No", "Zstack": "Yes", "Tseries": "No", "Passed QC": "Yes", "Comment": NaN, "FOV number": NaN, "FOVX": NaN, "FOVXUnit": NaN, "FOVY": NaN, "FOVYUnit": NaN, "Frame Averaging": NaN, "Image ID": "MAP03252_0000_06_04_005\\AFR\\MAP03252_0000_06_04_005_ERBB2_AFR.tif", "DimensionOrder": "XYCZT", "PhysicalSizeX": 0.325, "PhysicalSizeXUnit": "µm", "PhysicalSizeY": 0.325, "PhysicalSizeYUnit": "µm", "PhysicalSizeZ": 0, "PhysicalSizeZUnit": "µm", "Pixels BigEndian": false, "PlaneCount": 0, "SizeC": 26, "SizeT": 0, "SizeX": 9375, "SizeY": 9402, "SizeZ": 0, "PixelType": "uint16", "LEVEL": "2-Processed", "TYPE": "3-AFsubtracted", "LAYERS": 26, "REGION": 5, "POSITION": 0, "LAYER": 12, "ROUND": 17,


def parse_metadata_to_json(directory):
    result = []
    seen_rows = {}

    for file in files:
        if file.endswith(".tsv") or file.endswith(".csv"):
            file_path = os.path.join(directory, file)
            # regex match syn\d+ to get file_synid
            file_synid = None
            match = re.search(r"syn\d+", file)
            if match:
                file_synid = match.group(0)

            try:
                if file.endswith(".tsv"):
                    df = pd.read_csv(file_path, sep="\t", encoding="utf-8")
                else:
                    df = pd.read_csv(file_path, encoding="utf-8")
            except UnicodeDecodeError:
                try:
                    if file.endswith(".tsv"):
                        df = pd.read_csv(file_path, sep="\t", encoding="latin1")
                    else:
                        df = pd.read_csv(file_path, encoding="latin1")
                except Exception as e:
                    print(f"Error reading {file_path}: {e}")
                    continue

            for _, row in df.iterrows():
                row_content = row.to_dict()
                # drop the keys we don't need see above
                keys_to_drop = [
                    "Component",
                    "Filename",
                    "File Format",
                    "HTAN Participant ID",
                    "HTAN Parent Biospecimen ID",
                    "HTAN Data File ID",
                    "Channel Metadata Filename",
                    "Imaging Assay Type",
                    "Protocol Link",
                    "Workflow Start Datetime",
                    "Workflow End Datetime",
                    "Software and Version",
                    "Microscope",
                    "Objective",
                    "NominalMagnification",
                    "LensNA",
                    "WorkingDistance",
                    "WorkingDistanceUnit",
                    "Pyramid",
                    "Zstack",
                    "Tseries",
                    "Passed QC",
                    "Comment",
                    "FOV number",
                    "FOVX",
                    "FOVXUnit",
                    "FOVY",
                    "FOVYUnit",
                    "Frame Averaging",
                    "Image ID",
                    "DimensionOrder",
                    "PhysicalSizeX",
                    "PhysicalSizeXUnit",
                    "PhysicalSizeY",
                    "PhysicalSizeYUnit",
                    "PhysicalSizeZ",
                    "PhysicalSizeZUnit",
                    "Pixels BigEndian",
                    "PlaneCount",
                    "SizeC",
                    "SizeT",
                    "SizeX",
                    "SizeY",
                    "SizeZ",
                    "PixelType",
                    "LEVEL",
                    "TYPE",
                    "LAYERS",
                    "REGION",
                    "POSITION",
                    "LAYER",
                    "ROUND",
                ]

                for key in keys_to_drop:
                    row_content.pop(key, None)

                row_str = json.dumps(row_content, sort_keys=True)
                row_hash = hashlib.md5(row_str.encode("utf-8")).hexdigest()

                if row_hash in seen_rows:
                    # Add file path to existing entry
                    seen_rows[row_hash]["ch_synids"].append(file_synid)
                else:
                    # Create new entry
                    entry = {
                        "row_hash": row_hash,
                        "row_content": row_content,
                        "ch_synids": [file_synid],
                    }
                    seen_rows[row_hash] = entry
                    result.append(entry)

    return result


metadata_json = parse_metadata_to_json(directory)

print(f"Parsed {len(metadata_json)} unique rows from metadata files into JSON format.")


metadata_json_small = random.sample(metadata_json, 5)

metadata_df = pd.DataFrame(metadata_json_small)


# Test with one marker
print("Preparing marker entries...")
print(f"Found {len(metadata_df)} unique markers to curate")

# Test with metadata_df
for idx, row in tqdm(metadata_df.iterrows(), total=len(metadata_df)):
    marker_entry = {
        "extracted_marker": row["row_content"].get("Target Name", ""),
        "marker_type": "protein_single",  # Simplification for testing
        "row_contents": row["row_content"],
    }

    result = curate_marker_with_claude(marker_entry)
    print(f"Result for row {idx}:\n{result}\n\n")
