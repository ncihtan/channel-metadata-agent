"""
Simplified Claude Agent SDK Tutorial: UniProt Protein Lookup

This script demonstrates how to use the Claude Agent SDK with custom tools.
It shows a beginner-friendly example of creating an agentic workflow that:
1. Defines a custom tool for UniProt protein lookup
2. Lets Claude decide when to use the tool
3. Returns structured JSON output

Compare this with lookup_claude.py to see the difference between:
- Manual tool loop (lookup_claude.py)
- Automatic Agent SDK (this file)
"""

import asyncio
import json
import os
from typing import Any

import dotenv
from anthropic import transform_schema
from pydantic import BaseModel, Field
from enum import Enum

# Agent SDK imports
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    tool,
    create_sdk_mcp_server,
    AssistantMessage,
    TextBlock
)

# Import the existing UniProt API wrapper
from uniprot_api import lookup_protein

# Load environment variables (ANTHROPIC_API_KEY)
dotenv.load_dotenv()


# ============================================================================
# STEP 1: Define Pydantic Schemas for Structured Output
# ============================================================================

class MarkerTypeEnum(str, Enum):
    """Types of biological markers we can identify."""
    chemical_stain = "chemical_stain"
    blank_or_background = "blank_or_background"
    protein_group = "protein_group"
    protein_single = "protein_single"
    chemical_element = "chemical_element"
    cd_marker = "cd_marker"
    other_dna = "other_dna"
    other = "other"


class SubcellularLocationEnum(str, Enum):
    """Simplified subcellular locations."""
    cytoplasm = "cytoplasm"
    nucleus = "nucleus"
    cell_membrane = "cell_membrane"
    extracellular = "extracellular"


class ExtractedMarkerSchema(BaseModel):
    """Schema for the extracted marker information."""
    extracted_marker: str = Field(
        ...,
        description="The most relevant canonical marker or identifier for the protein, stain or feature being imaged extracted from the metadata row content.",
    )
    marker_type: MarkerTypeEnum = Field(
        ..., description="The type of the canonical marker."
    )


class UniprotOutputSchema(BaseModel):
    """Schema for UniProt protein entry."""
    accession: str = Field(..., description="The UniProt accession ID of the protein.")
    gene_name: str = Field(
        ..., description="The gene name associated with the protein."
    )
    protein_name: str = Field(..., description="The full name of the protein.")
    organism: str = Field(
        ..., description="The organism from which the protein is derived."
    )
    subcellular_location: SubcellularLocationEnum = Field(
        ..., description="Subcellular location where the protein is found."
    )
    confidence_score: float = Field(
        ..., description="Confidence score of the protein match (0 to 1)."
    )


class MarkerUniprotSchema(ExtractedMarkerSchema):
    """Combined schema with marker info and UniProt entry."""
    uniprot_entry: UniprotOutputSchema | None = Field(
        None, description="The selected UniProt entry for the marker."
    )


# ============================================================================
# STEP 2: Define Custom Tool for UniProt Lookup
# ============================================================================

@tool(
    name="lookup_protein",
    description="Searches the UniProt database for protein information by gene name or protein name. Returns a list of matching protein entries with details like gene name, protein name, organism, subcellular location, function, and confidence score. Use this to find candidate proteins that match the marker.",
    input_schema={
        "query": str,
        "organism": str  # Default will be handled in function body
    }
)
async def lookup_protein_tool(args: dict[str, Any]) -> dict[str, Any]:
    """
    Custom tool wrapper for UniProt protein lookup.

    This function:
    1. Receives arguments from Claude (query and organism)
    2. Calls the existing lookup_protein function from uniprot_api.py
    3. Formats results as text for Claude to analyze
    4. Returns in the format expected by the Agent SDK
    """
    query = args["query"]
    organism = args.get("organism", "human")  # Default to human

    print(f"[TOOL] Looking up protein: '{query}' in organism: '{organism}'")

    # Call the existing UniProt API wrapper
    results = lookup_protein(query, organism)

    # Format results as text for Claude to read
    if not results:
        return {
            "content": [{
                "type": "text",
                "text": f"No UniProt entries found for '{query}' in {organism}."
            }]
        }

    # Build a formatted text response with all results
    result_text = f"Found {len(results)} UniProt entries for '{query}':\n\n"
    for i, entry in enumerate(results, 1):
        result_text += f"Result {i}:\n"
        result_text += f"  Accession: {entry.accession}\n"
        result_text += f"  Gene: {entry.gene_name}\n"
        result_text += f"  Protein: {entry.protein_name}\n"
        result_text += f"  Organism: {entry.organism}\n"
        result_text += f"  Confidence: {entry.confidence_score}\n"
        if entry.subcellular_location:
            result_text += f"  Location: {', '.join(entry.subcellular_location)}\n"
        if entry.function:
            # Truncate long function descriptions
            result_text += f"  Function: {entry.function[:300]}...\n"
        result_text += "\n"

    return {
        "content": [{
            "type": "text",
            "text": result_text
        }]
    }


# ============================================================================
# STEP 3: Create MCP Server with Custom Tool
# ============================================================================

# Create an MCP server that hosts our custom tool
# The Agent SDK will register this server and make the tool available to Claude
uniprot_server = create_sdk_mcp_server(
    name="uniprot",
    version="1.0.0",
    tools=[lookup_protein_tool]  # Pass our decorated tool function
)


# ============================================================================
# STEP 4: Main Function Using Agent SDK
# ============================================================================

async def curate_marker_with_claude(marker_entry: dict) -> str:
    """
    Use Claude Agent SDK to intelligently curate a protein marker.

    This function demonstrates the Agent SDK pattern:
    1. Configure options with custom tools and structured output
    2. Create ClaudeSDKClient for agentic interaction
    3. Send a prompt and let Claude decide when to use tools
    4. Receive structured JSON output

    Args:
        marker_entry: Dictionary with 'extracted_marker', 'marker_type', and 'row_contents'

    Returns:
        JSON string with curated marker information
    """
    extracted_marker = marker_entry.get("extracted_marker", "")
    marker_type = marker_entry.get("marker_type", "")
    row_contents = marker_entry.get("row_contents", {})

    # Build a rich prompt with context
    user_prompt = f"""I need you to find the correct UniProt entry for this marker used in imaging:

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

    # Configure Agent SDK options
    options = ClaudeAgentOptions(
        # Register our MCP server with custom tools
        mcp_servers={"uniprot": uniprot_server},

        # Allow Claude to use our custom tool
        # Note: Tool names are prefixed with "mcp__servername__toolname"
        allowed_tools=["mcp__uniprot__lookup_protein"],

        # Use Claude Sonnet 4.5 model
        model="claude-sonnet-4-5",

        # Auto-approve tool usage (no manual confirmation)
        permission_mode="bypassPermissions",

        # Request structured JSON output matching our schema
        output_format={
            "type": "json_schema",
            "schema": transform_schema(MarkerUniprotSchema)
        }
    )

    # Use ClaudeSDKClient for the agentic interaction
    # The SDK handles the entire tool loop automatically
    async with ClaudeSDKClient(options=options) as client:
        # Send the prompt - Claude will decide if/when to use tools
        await client.query(user_prompt)

        # Collect the response
        all_text_blocks = []
        async for message in client.receive_response():
            # Debug: print message type
            print(f"[DEBUG] Message type: {type(message).__name__}")

            if isinstance(message, AssistantMessage):
                for block in message.content:
                    print(f"[DEBUG] Block type: {type(block).__name__}")
                    if isinstance(block, TextBlock):
                        # Collect all text blocks
                        all_text_blocks.append(block.text)
                        print(f"[DEBUG] Text block {len(all_text_blocks)}: {len(block.text)} chars")
                        print(f"[DEBUG] Preview: {block.text[:100]}...")

        # Try to find JSON in the text blocks
        result_json = None
        for i, text in enumerate(all_text_blocks):
            try:
                # Try to parse as JSON
                json.loads(text)
                print(f"[DEBUG] Found valid JSON in block {i+1}")
                result_json = text
                break
            except json.JSONDecodeError:
                continue

        if result_json is None:
            print("[WARNING] No valid JSON found in text blocks")
            print(f"[DEBUG] Total text blocks: {len(all_text_blocks)}")
            if all_text_blocks:
                print(f"[DEBUG] Last block content: {all_text_blocks[-1]}")
            return '{"error": "No structured JSON response received"}'

        return result_json


# ============================================================================
# STEP 5: Example Usage
# ============================================================================

async def main():
    """
    Simple example demonstrating the Agent SDK workflow.
    """
    print("=" * 70)
    print("Claude Agent SDK Tutorial: UniProt Protein Lookup")
    print("=" * 70)
    print()

    # Example marker entry with imaging metadata
    # This simulates a row from a multiplex imaging experiment
    test_marker = {
        "extracted_marker": "CD20",
        "marker_type": "protein_single",
        "row_contents": {
            "Target Name": "CD20",
            "Antibody Clone": "L26",
            "Antibody Vendor": "Abcam",
            "Catalog Number": "ab9475",
            "Host Species": "Mouse",
            "Antibody Type": "Monoclonal",
            "Tissue": "Lymph node",
            "Assay Type": "Multiplex Immunofluorescence"
        }
    }

    print(f"Test Marker: {test_marker['extracted_marker']}")
    print(f"Marker Type: {test_marker['marker_type']}")
    print()
    print("Processing with Agent SDK...")
    print("-" * 70)
    print()

    # Call the Agent SDK function
    result = await curate_marker_with_claude(test_marker)

    # Display results
    print()
    print("=" * 70)
    print("RESULT (Structured JSON Output):")
    print("=" * 70)

    # Pretty-print the JSON result
    try:
        result_dict = json.loads(result)
        print(json.dumps(result_dict, indent=2))
    except json.JSONDecodeError:
        print(result)

    print()
    print("=" * 70)
    print("✓ Tutorial Complete!")
    print()
    print("Key concepts demonstrated:")
    print("1. Custom tool definition with @tool decorator")
    print("2. MCP server creation with create_sdk_mcp_server()")
    print("3. ClaudeSDKClient for automatic agentic loop")
    print("4. Structured output with JSON schema validation")
    print("5. No manual tool loop - Claude handles everything!")
    print("=" * 70)


if __name__ == "__main__":
    # Run the async main function
    asyncio.run(main())
