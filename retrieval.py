"""Policy retrieval: chunking, mocked embeddings, vector search.

A real pipeline embeds via a hosted model (Voyage, OpenAI) in `_embed` — a
network call needing its own API key, unjustified for a ~20-paragraph corpus.
What's mocked is only the vectors themselves: `_embed` is a deterministic
local stand-in (IDF-weighted feature hashing, no learned semantics) rather
than a real embedding model. Everything downstream of it is the real thing a
RAG pipeline actually does — chunks are embedded once at import time into a
fixed-length vector store, the query is embedded the same way at request
time, and `search` is nearest-neighbor by cosine similarity, not string
matching. Swapping in a real embedding model later means replacing `_embed`'s
body; nothing else here changes.
"""

import hashlib
import math
import os
import re
from typing import Dict, List

POLICY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "policies")

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "do", "does", "did", "i",
    "my", "me", "you", "your", "it", "to", "for", "of", "on", "in", "and",
    "or", "how", "what", "when", "can", "could", "would", "should", "if",
    "this", "that", "with", "have", "has", "get", "got",
}

_SUFFIXES = ["ies", "ing", "able", "ed", "es", "s"]


def _stem(word: str) -> str:
    """Crude suffix-stripping stemmer — no linguistic ambition, just enough
    that 'returnable'/'returned'/'returns' land on the same root as a query
    that says 'return'. A real embedding model wouldn't need this; a hashed
    bag-of-words does, since it only ever matches on identical tokens."""
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -3] + "y" if suf == "ies" else word[: -len(suf)]
    return word


def _tokens(s: str) -> set:
    """Bag of words, each kept alongside its stem so exact and stemmed
    queries both match ('refunds' and 'refund' both hit a chunk about
    refunds)."""
    out = set()
    for word in re.findall(r"[a-z']+", s.lower()):
        if word in _STOPWORDS:
            continue
        out.add(word)
        out.add(_stem(word))
    return out


def _load_policies() -> List[Dict]:
    docs = []
    for name in sorted(os.listdir(POLICY_DIR)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(POLICY_DIR, name)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        # One chunk per paragraph keeps the grounding tight. Bare markdown
        # headers carry no retrievable content of their own and are noisy
        # matches for a lexical vector (a title's few words can outweigh a
        # real paragraph's), so they're dropped as chunks.
        for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
            if para.startswith("#") and "\n" not in para:
                continue
            docs.append({"source": name, "text": para})
    return docs


_POLICY_CHUNKS = _load_policies()


def _build_idf(chunks: List[Dict]) -> Dict[str, float]:
    """Rare words carry the signal; 'order' and 'book' are everywhere."""
    n = len(chunks)
    df: Dict[str, int] = {}
    for chunk in chunks:
        for token in _tokens(chunk["text"]):
            df[token] = df.get(token, 0) + 1
    return {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}


_IDF = _build_idf(_POLICY_CHUNKS)

EMBED_DIM = 256


def _embed(text: str) -> List[float]:
    """Feature-hashed, IDF-weighted bag-of-words, L2-normalized.

    Standard "hashing trick" (as in sklearn's HashingVectorizer): each token
    hashes to one of EMBED_DIM dimensions with a signed weight, so unrelated
    tokens partially cancel instead of only ever adding up. It's still
    lexical, not semantic — the honest limit of mocking this without a real
    model — but it's a genuine fixed-length vector, not a keyword-overlap
    score.
    """
    vec = [0.0] * EMBED_DIM
    for token in _tokens(text):
        digest = int(hashlib.md5(token.encode()).hexdigest(), 16)
        idx = digest % EMBED_DIM
        sign = 1.0 if (digest // EMBED_DIM) % 2 == 0 else -1.0
        vec[idx] += sign * _IDF.get(token, 1.0)
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _cosine(a: List[float], b: List[float]) -> float:
    # Both sides are already L2-normalized, so the dot product is the cosine
    # similarity directly.
    return sum(x * y for x, y in zip(a, b))


# The vector store: every chunk embedded once, held in memory for the
# process's lifetime — same pattern as _POLICY_CHUNKS/_IDF, just vectors
# instead of a token index. A real deployment would swap the in-memory list
# for a vector DB (FAISS, pgvector) once the corpus outgrew a linear scan;
# not warranted at this size.
_CHUNK_VECTORS = [(_embed(c["text"]), c) for c in _POLICY_CHUNKS]

# Cosine similarity lives in [-1, 1]; tuned by hand against the queries in
# test_tools.py, the same way an IDF-overlap threshold would be tuned.
RETRIEVAL_MIN_SIMILARITY = 0.15


def search(query: str, k: int = 3) -> List[Dict]:
    """Embed the query, rank policy chunks by cosine similarity, return the
    top k above RETRIEVAL_MIN_SIMILARITY as {source, text} dicts.

    An empty list means no confident match — the caller's job is to say so,
    not to guess from whatever scored highest anyway.
    """
    query_vec = _embed(query)
    scored = [(_cosine(query_vec, chunk_vec), chunk)
              for chunk_vec, chunk in _CHUNK_VECTORS]
    scored = [pair for pair in scored if pair[0] >= RETRIEVAL_MIN_SIMILARITY]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [{"source": c["source"], "text": c["text"]} for _, c in scored[:k]]
