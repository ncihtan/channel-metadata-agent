"""
UniProt API Tool for antibody target lookup and validation.
Implements caching, batch queries, and retry logic.
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type
)


@dataclass
class UniProtEntry:
    """Represents a UniProt protein entry."""
    accession: str
    gene_name: Optional[str]
    protein_name: str
    organism: str
    subcellular_location: List[str]
    function: Optional[str]
    confidence_score: float

    def to_dict(self) -> Dict:
        return asdict(self)


class UniProtCache:
    """SQLite-based cache for UniProt queries."""

    def __init__(self, cache_dir: Path = Path("data/cache")):
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = cache_dir / "uniprot_cache.db"
        self._init_db()

    def _init_db(self):
        """Initialize the cache database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS uniprot_cache (
                    query TEXT PRIMARY KEY,
                    response TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    ttl_days INTEGER DEFAULT 30
                )
            """)
            conn.commit()

    def get(self, query: str) -> Optional[Dict]:
        """Retrieve cached result if not expired."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT response, timestamp, ttl_days FROM uniprot_cache WHERE query = ?",
                (query,)
            )
            row = cursor.fetchone()

            if row:
                response, timestamp, ttl_days = row
                cached_time = datetime.fromisoformat(timestamp)

                if datetime.now() - cached_time < timedelta(days=ttl_days):
                    return json.loads(response)

        return None

    def set(self, query: str, response: Dict, ttl_days: int = 30):
        """Cache a query response."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO uniprot_cache (query, response, timestamp, ttl_days)
                VALUES (?, ?, ?, ?)
                """,
                (query, json.dumps(response), datetime.now().isoformat(), ttl_days)
            )
            conn.commit()

    def clear_expired(self):
        """Remove expired cache entries."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                DELETE FROM uniprot_cache
                WHERE datetime(timestamp, '+' || ttl_days || ' days') < datetime('now')
            """)
            conn.commit()


class UniProtAPI:
    """
    UniProt REST API client with retry logic and caching.
    """

    BASE_URL = "https://rest.uniprot.org"

    def __init__(self, cache_dir: Path = Path("data/cache")):
        self.cache = UniProtCache(cache_dir)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'AntibodyCurationAgent/1.0 (ataylor@example.com)'
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.RequestException, requests.Timeout))
    )
    def _make_request(self, endpoint: str, params: Dict) -> requests.Response:
        """Make HTTP request with retry logic."""
        url = f"{self.BASE_URL}/{endpoint}"
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response

    def search_protein(
        self,
        query: str,
        organism: str = "human",
        reviewed: bool = True
    ) -> List[UniProtEntry]:
        """
        Search for proteins by gene name or protein name.

        Args:
            query: Gene name or protein name to search
            organism: Organism filter (default: human)
            reviewed: Only return reviewed (Swiss-Prot) entries

        Returns:
            List of UniProtEntry objects with confidence scores
        """
        # Check cache first
        cache_key = f"search:{query}:{organism}:{reviewed}"
        cached = self.cache.get(cache_key)

        if cached:
            return [UniProtEntry(**entry) for entry in cached]

        # Build search query
        search_query = f"{query}"
        if organism:
            search_query += f" AND (organism_name:{organism})"
        if reviewed:
            search_query += " AND (reviewed:true)"

        params = {
            'query': search_query,
            'format': 'json',
            'fields': 'accession,gene_names,protein_name,organism_name,cc_subcellular_location,cc_function',
            'size': 10
        }

        try:
            response = self._make_request('uniprotkb/search', params)
            data = response.json()

            entries = []
            for result in data.get('results', []):
                entry = self._parse_entry(result, query)
                entries.append(entry)

            # Cache results
            self.cache.set(cache_key, [e.to_dict() for e in entries])

            return entries

        except requests.RequestException as e:
            print(f"UniProt API error for query '{query}': {e}")
            return []

    def get_by_accession(self, accession: str) -> Optional[UniProtEntry]:
        """
        Retrieve protein entry by UniProt accession ID.

        Args:
            accession: UniProt accession (e.g., 'P12345')

        Returns:
            UniProtEntry object or None if not found
        """
        cache_key = f"accession:{accession}"
        cached = self.cache.get(cache_key)

        if cached:
            return UniProtEntry(**cached)

        try:
            response = self._make_request(
                f'uniprotkb/{accession}',
                params={'format': 'json'}
            )
            data = response.json()

            entry = self._parse_entry(data, accession, confidence=1.0)

            # Cache result
            self.cache.set(cache_key, entry.to_dict())

            return entry

        except requests.RequestException as e:
            print(f"UniProt API error for accession '{accession}': {e}")
            return None

    def batch_search(
        self,
        queries: List[str],
        organism: str = "human",
        delay: float = 0.2
    ) -> Dict[str, List[UniProtEntry]]:
        """
        Batch search multiple proteins with rate limiting.

        Args:
            queries: List of gene/protein names
            organism: Organism filter
            delay: Delay between requests (seconds)

        Returns:
            Dictionary mapping queries to results
        """
        results = {}

        for query in queries:
            results[query] = self.search_protein(query, organism)
            time.sleep(delay)  # Rate limiting

        return results

    def _parse_entry(
        self,
        data: Dict,
        query: str,
        confidence: Optional[float] = None
    ) -> UniProtEntry:
        """Parse UniProt API response into UniProtEntry."""

        # Extract gene name
        gene_name = None
        if 'genes' in data and data['genes']:
            gene_name = data['genes'][0].get('geneName', {}).get('value')

        # Extract protein name
        protein_name = "Unknown"
        if 'proteinDescription' in data:
            rec_name = data['proteinDescription'].get('recommendedName', {})
            protein_name = rec_name.get('fullName', {}).get('value', 'Unknown')

        # Extract subcellular location
        subcellular = []
        if 'comments' in data:
            for comment in data['comments']:
                if comment.get('commentType') == 'SUBCELLULAR LOCATION':
                    for loc in comment.get('subcellularLocations', []):
                        location = loc.get('location', {}).get('value')
                        if location:
                            subcellular.append(location)

        # Extract function
        function = None
        if 'comments' in data:
            for comment in data['comments']:
                if comment.get('commentType') == 'FUNCTION':
                    texts = comment.get('texts', [])
                    if texts:
                        function = texts[0].get('value')
                        break

        # Calculate confidence score if not provided
        if confidence is None:
            confidence = self._calculate_confidence(data, query)

        return UniProtEntry(
            accession=data.get('primaryAccession', ''),
            gene_name=gene_name,
            protein_name=protein_name,
            organism=data.get('organism', {}).get('scientificName', ''),
            subcellular_location=subcellular,
            function=function,
            confidence_score=confidence
        )

    def _calculate_confidence(self, data: Dict, query: str) -> float:
        """Calculate confidence score for a match."""
        score = 0.5  # Base score

        query_lower = query.lower()

        # Boost if gene name matches
        if 'genes' in data and data['genes']:
            gene_name = data['genes'][0].get('geneName', {}).get('value', '').lower()
            if gene_name == query_lower:
                score += 0.4
            elif query_lower in gene_name or gene_name in query_lower:
                score += 0.2

        # Boost if reviewed (Swiss-Prot)
        if data.get('entryType') == 'UniProtKB reviewed (Swiss-Prot)':
            score += 0.1

        return min(score, 1.0)


# Convenience function
def lookup_protein(
    query: str,
    organism: str = "human",
    cache_dir: Path = Path("data/cache")
) -> List[UniProtEntry]:
    """
    Quick lookup function for protein information.

    Args:
        query: Gene or protein name
        organism: Target organism
        cache_dir: Cache directory path

    Returns:
        List of matching UniProt entries
    """
    api = UniProtAPI(cache_dir)
    return api.search_protein(query, organism)
