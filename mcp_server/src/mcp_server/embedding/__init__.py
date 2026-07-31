"""Embedding layer: BGE-M3 (GPU + CPU fallback), tokenizer."""

from .manager import EmbeddingManager
from .tokenizer import XlmRobertaTokenizer, tokenizer as xlmr_tokenizer

__all__ = ["EmbeddingManager", "XlmRobertaTokenizer", "xlmr_tokenizer"]
