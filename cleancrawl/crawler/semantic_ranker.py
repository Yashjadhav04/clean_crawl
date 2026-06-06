"""
Semantic Ranking Module — TF-IDF + Cosine Similarity

Lightweight semantic similarity without heavy ML dependencies.
Used to:
  1. Re-rank crawled articles by semantic closeness to the query
  2. Cluster similar articles together
  3. Find articles "about the same topic" even with different wording

Uses character n-grams + word tokens for robustness across languages
and spelling variations.
"""
import math
import re
from collections import Counter
from typing import Optional


# Multilingual stop words (English first, common others)
STOP_WORDS = {
    # English
    "the","a","an","and","or","but","in","on","at","to","for","of","with","by",
    "from","is","it","that","this","was","are","be","has","had","have","not",
    "they","we","you","he","she","its","as","which","their","will","can","would",
    "could","been","said","than","more","also","were","about","into","all","one",
    "two","new","other","may","who","what","when","where","how","if","do","did",
    "does","just","over","such","like","most","some","very","much","only","so","no",
    "up","out","any","each","use","used","using","get","make","made","need","want",
    "your","our","there","here","then","now","time","way","well","even","back",
    "first","last","long","great","good","just","take","see","know","think",
    # Common Spanish
    "el","la","de","y","en","los","las","un","una","es","por","con","para",
    # Common French
    "le","les","des","du","au","aux","est","pour","avec","dans","sur","par","ce",
    # Common German
    "der","die","das","den","und","ist","mit","von","für","auf","ein","eine",
}


def _tokenize(text: str, lower: bool = True) -> list[str]:
    """Word tokens — handles unicode for multilingual support."""
    if lower:
        text = text.lower()
    # Word-level tokens (unicode-aware)
    tokens = re.findall(r"[\w]{3,}", text, re.UNICODE)
    return [t for t in tokens if t not in STOP_WORDS]


def _char_ngrams(text: str, n: int = 4) -> list[str]:
    """Character n-grams for fuzzy matching (handles typos/spelling)."""
    text = re.sub(r"\s+", " ", text.lower())
    return [text[i:i+n] for i in range(len(text) - n + 1)]


def build_vocab(documents: list[str]) -> dict:
    """Build IDF-weighted vocabulary from a corpus."""
    n_docs = len(documents)
    doc_freq: Counter = Counter()
    for doc in documents:
        unique = set(_tokenize(doc))
        for term in unique:
            doc_freq[term] += 1

    # IDF weights
    idf: dict[str, float] = {}
    for term, df in doc_freq.items():
        idf[term] = math.log((n_docs + 1) / (df + 1)) + 1

    return idf


def tfidf_vector(text: str, idf: dict) -> dict[str, float]:
    """Convert text to TF-IDF sparse vector."""
    tokens = _tokenize(text)
    if not tokens:
        return {}

    tf = Counter(tokens)
    total = sum(tf.values())

    vector: dict[str, float] = {}
    for term, count in tf.items():
        tf_val = count / total
        idf_val = idf.get(term, math.log(100))  # default IDF for OOV
        vector[term] = tf_val * idf_val

    # L2 normalize
    norm = math.sqrt(sum(v * v for v in vector.values()))
    if norm > 0:
        vector = {k: v / norm for k, v in vector.items()}

    return vector


def cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity between two sparse vectors."""
    if not a or not b:
        return 0.0
    # Use smaller dict for iteration
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0) for k, v in a.items())


def jaccard_char_ngrams(a: str, b: str, n: int = 4) -> float:
    """Jaccard similarity on character n-grams — fuzzy string matching."""
    set_a = set(_char_ngrams(a, n))
    set_b = set(_char_ngrams(b, n))
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


class SemanticRanker:
    """
    Semantic ranker for a fixed query. Builds vocab from candidate documents,
    then scores each one against the query.
    """

    def __init__(self, query: str, documents: list[str]):
        self.query = query
        # Build vocab including the query so query terms are in the IDF
        corpus = [query] + documents
        self.idf = build_vocab(corpus)
        self.query_vec = tfidf_vector(query, self.idf)
        self.query_chars = set(_char_ngrams(query.lower(), 4))

    def score(self, document: str, title: str = "") -> dict:
        """
        Score a document against the query. Returns:
          - cosine: TF-IDF cosine similarity (semantic)
          - jaccard: char n-gram Jaccard (fuzzy)
          - title_match: title-only Jaccard
          - combined: weighted final score (0-1)
        """
        # Bias toward title — titles are dense in topic words
        full_text = (title + " ") * 3 + document[:5000]
        doc_vec = tfidf_vector(full_text, self.idf)

        cosine = cosine_similarity(self.query_vec, doc_vec)
        jaccard = jaccard_char_ngrams(self.query, document[:1000])
        title_match = jaccard_char_ngrams(self.query, title) if title else 0.0

        # Combined: cosine (50%) + jaccard (20%) + title match (30%)
        combined = cosine * 0.5 + jaccard * 0.2 + title_match * 0.3

        return {
            "cosine": round(cosine, 4),
            "jaccard": round(jaccard, 4),
            "title_match": round(title_match, 4),
            "combined": round(min(combined, 1.0), 4),
        }

    def rank(self, documents: list[tuple[str, str]]) -> list[tuple[int, dict]]:
        """Rank a list of (title, body) tuples. Returns sorted (index, scores)."""
        scored = []
        for i, (title, body) in enumerate(documents):
            scores = self.score(body, title)
            scored.append((i, scores))
        scored.sort(key=lambda x: -x[1]["combined"])
        return scored


def cluster_similar_articles(documents: list[str], threshold: float = 0.35) -> list[list[int]]:
    """
    Cluster articles that are semantically similar.
    Simple agglomerative clustering using cosine similarity.
    Returns list of clusters (each cluster is a list of document indices).
    """
    if not documents:
        return []

    idf = build_vocab(documents)
    vectors = [tfidf_vector(d, idf) for d in documents]

    clusters: list[list[int]] = []
    assigned: set[int] = set()

    for i in range(len(documents)):
        if i in assigned:
            continue
        cluster = [i]
        assigned.add(i)
        for j in range(i + 1, len(documents)):
            if j in assigned:
                continue
            sim = cosine_similarity(vectors[i], vectors[j])
            if sim >= threshold:
                cluster.append(j)
                assigned.add(j)
        clusters.append(cluster)

    return clusters
