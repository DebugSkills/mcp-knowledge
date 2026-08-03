"""Keyword extraction — TF-IDF для авто-тегов (advisory).

Фаза 5 §6.4: yake НЕ доступен, nltk НЕ доступен.
Используем sklearn TfidfVectorizer для извлечения top-N ключевых слов.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from sklearn.feature_extraction.text import TfidfVectorizer

logger = logging.getLogger("mcp_knowledge.content.keywords")

# ── Стоп-слова RU + EN ────────────────────────────────────

_RU_STOP_WORDS: set[str] = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а",
    "то", "все", "она", "так", "но", "да", "ты", "к", "у", "же", "вы",
    "за", "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня",
    "еще", "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну",
    "вдруг", "ли", "если", "уже", "или", "ни", "быть", "был", "него",
    "до", "вас", "нибудь", "опять", "уж", "вам", "ведь", "там", "потом",
    "себя", "ничего", "ей", "может", "они", "тут", "где", "есть", "надо",
    "ней", "для", "мы", "тебя", "их", "чем", "была", "сам", "чтоб",
    "без", "будто", "чего", "раз", "тоже", "себе", "под", "будет",
    "это", "этот", "эта", "эти", "этого", "этой", "этому", "этим",
    "кто", "той", "которая", "которые", "который", "которых",
}

_EN_STOP_WORDS: set[str] = {
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i",
    "it", "for", "not", "on", "with", "he", "as", "you", "do", "at",
    "this", "but", "his", "by", "from", "they", "we", "say", "her",
    "she", "or", "an", "will", "my", "one", "all", "would", "there",
    "their", "what", "so", "up", "out", "if", "about", "who", "get",
    "which", "go", "me", "when", "make", "can", "like", "time", "no",
    "just", "him", "know", "take", "people", "into", "year", "your",
    "good", "some", "could", "them", "see", "other", "than", "then",
    "now", "look", "only", "come", "its", "over", "think", "also",
    "back", "use", "two", "how", "our", "work", "first", "well",
    "way", "even", "new", "want", "any", "these", "give", "most",
    "is", "are", "was", "were", "been", "being", "has", "had",
    "having", "does", "did", "doing", "etc", "per", "via",
}


def _normalize_tag(word: str) -> str:
    """Нормализация в kebab-case тег: lowercase, транслит, очистка."""
    # Простой транслит кириллицы в латиницу
    translit_map = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    }
    result = word.lower()
    for cyr, lat in translit_map.items():
        result = result.replace(cyr, lat)
    # Удаляем спецсимволы, оставляем буквы, цифры, дефисы
    result = re.sub(r"[^a-z0-9-]", "", result)
    result = re.sub(r"-{2,}", "-", result)
    result = result.strip("-")
    return result


def _is_stop_word(word: str) -> bool:
    """Проверить, является ли слово стоп-словом (RU или EN)."""
    return word.lower() in _RU_STOP_WORDS or word.lower() in _EN_STOP_WORDS


def extract_keywords(
    texts: list[str],
    top_n: int = 5,
    max_features: int = 100,
    stop_words: Optional[set[str]] = None,
) -> list[list[str]]:
    """Извлечь top-N ключевых слов на каждый текст через TF-IDF.

    Корпус = все тексты (секции книги). TF-IDF вычисляется относительно корпуса.
    Для каждого документа выбираются слова с максимальным TF-IDF score.

    Args:
        texts: список текстов секций
        top_n: количество ключевых слов на секцию (default 5)
        max_features: максимальное число фич для TfidfVectorizer
        stop_words: дополнительные стоп-слова (объединяются с RU+EN)

    Returns:
        list[list[str]]: топ-N тегов (kebab-case) для каждого текста
    """
    if not texts:
        return []

    # Собираем стоп-слова
    stops = _RU_STOP_WORDS | _EN_STOP_WORDS
    if stop_words:
        stops = stops | stop_words

    # Фильтруем пустые тексты
    non_empty = [t for t in texts if t.strip()]
    if not non_empty:
        return [[] for _ in texts]

    try:
        vectorizer = TfidfVectorizer(
            max_features=max_features,
            stop_words=list(stops),
            lowercase=True,
            token_pattern=r"(?u)\b[a-zа-яё]{3,}\b",  # слова ≥ 3 букв
        )
        tfidf_matrix = vectorizer.fit_transform(non_empty)
        feature_names = vectorizer.get_feature_names_out()
    except ValueError:
        # Слишком мало/короткие тексты — возвращаем пустые списки
        return [[] for _ in texts]

    # Для каждого текста: извлекаем top-N фич с максимальным TF-IDF
    results: list[list[str]] = []
    empty_count = 0

    for i, text in enumerate(texts):
        if not text.strip():
            results.append([])
            empty_count += 1
            continue

        doc_idx = i - empty_count
        if doc_idx < 0 or doc_idx >= tfidf_matrix.shape[0]:
            results.append([])
            continue

        row = tfidf_matrix[doc_idx]
        if row.nnz == 0:
            results.append([])
            continue

        # Сортируем индексы по убыванию TF-IDF score
        scores = row.toarray().flatten()
        top_indices = scores.argsort()[::-1][:top_n]

        keywords = []
        for idx in top_indices:
            if scores[idx] > 0:
                word = feature_names[idx]
                if not _is_stop_word(word):
                    tag = _normalize_tag(word)
                    if tag and len(tag) >= 3:
                        keywords.append(tag)

        results.append(keywords[:top_n])

    return results


def deduplicate_tags(
    auto_tags: list[str],
    inherited_tags: list[str],
) -> list[str]:
    """Дедупликация: авто-теги ∪ inherited (inherited первыми).

    Убираем дубликаты, сохраняя порядок: inherited → auto.
    """
    seen = set()
    result = []
    for tag in inherited_tags:
        norm = tag.lower()
        if norm not in seen:
            seen.add(norm)
            result.append(tag)
    for tag in auto_tags:
        norm = tag.lower()
        if norm not in seen:
            seen.add(norm)
            result.append(tag)
    return result
