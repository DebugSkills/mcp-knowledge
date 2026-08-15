"""Unit-тесты dup_ranking.py — таблица R1-R6 (Фаза 2 dedup).

Проверяют двухпоточную модель 🟢/🟡/🔴: exact content-hash → 🟢,
антонимы → 🟡 (контрпример Critic), low-cosine → 🔴, canonical.
"""

from __future__ import annotations

from mcp_server.quality.dup_ranking import (
    extract_target_kid,
    has_negation_pattern,
    is_r1_exact_hash,
    rank_pair,
    recommend_canonical,
    rel_length_diff,
    summarize_signals,
)


def _meta(**overrides) -> dict:
    """Базовые сигналы dup-пары (структура Фазы 1 + target_subject Фазы 3)."""
    base = {
        "cosine": 0.95,
        "content_hash": "aaa111",
        "content_length": 300,
        "target_content_hash": "bbb222",
        "target_content_length": 300,
        "slug_negation": False,
        "standalone": True,
        "target_standalone": True,
        "subject": "devops",
        "target_subject": "devops",  # Фаза 3 (0a): реальное сравнение subject
        "target_kid": "tgt-kid",
    }
    base.update(overrides)
    return base


class TestRankPair:
    """Таблица решений R1-R6."""

    def test_r1_hash_equal_standalone_green(self):
        """R1: exact content-hash + standalone + same subject → 🟢."""
        assert rank_pair(_meta(
            content_hash="abc", target_content_hash="abc",
        )) == "green"

    def test_r1_hash_match_different_subject_yellow(self):
        """R1: exact hash + standalone, но РАЗНЫЙ subject → НЕ green (🟡)."""
        assert rank_pair(_meta(
            content_hash="abc", target_content_hash="abc",
            subject="devops", target_subject="other",
        )) == "yellow"

    def test_r1_hash_match_legacy_no_target_subject_yellow(self):
        """R1: exact hash + standalone, но БЕЗ target_subject (легаси) → 🟡."""
        assert rank_pair(_meta(
            content_hash="abc", target_content_hash="abc",
            target_subject=None,
        )) == "yellow"

    def test_r1_hash_match_before_cosine_gate(self):
        """Фаза 3 (2b): exact-hash пара с cosine<0.92 → 🟢 (hash сильнее cosine)."""
        assert rank_pair(_meta(
            content_hash="abc", target_content_hash="abc",
            cosine=0.80,
        )) == "green"

    def test_r2_hash_equal_with_parent_yellow(self):
        """R2: hash совпал, но не standalone → 🟡 (cross-collection риск)."""
        assert rank_pair(_meta(
            content_hash="abc", target_content_hash="abc",
            standalone=False, target_standalone=True,
        )) == "yellow"

    def test_r3_cosine_097_no_negation_green(self):
        """R3: cosine 0.98 + lengths ok + standalone + не антоним → 🟢."""
        assert rank_pair(_meta(
            cosine=0.98, content_length=100, target_content_length=105,
        )) == "green"

    def test_r4_negation_yellow(self):
        """R4: антоним-пара (контрпример Critic) → 🟡, НЕ 🟢."""
        # chto-lyubit-ai ≈ chto-ne-lyubit-ai: cosine высокий, но slug_negation
        assert rank_pair(_meta(
            cosine=0.995, slug_negation=True, content_length=300, target_content_length=300,
        )) == "yellow"

    def test_r4_len_diff_yellow(self):
        """R4: cosine 0.95 + большая разница длин → 🟡."""
        assert rank_pair(_meta(
            cosine=0.95, content_length=100, target_content_length=500,
        )) == "yellow"

    def test_r5_len_diff_wide_yellow(self):
        """R5: cosine ≥ 0.92 + hash mismatch + len diff > 0.30 → 🟡."""
        assert rank_pair(_meta(
            cosine=0.93, content_length=100, target_content_length=700,
        )) == "yellow"

    def test_r6_low_cosine_red(self):
        """R6: cosine < 0.92 → 🔴 (косвенное)."""
        assert rank_pair(_meta(cosine=0.80)) == "red"

    def test_fallback_from_detail_no_metadata(self):
        """Пустая metadata → cosine из detail; ≥0.92 → 🟡, <0.92 → 🔴."""
        detail = "Possible duplicate of kid-x (same subject=devops, cosine=0.95)"
        assert rank_pair(None, detail) == "yellow"
        detail_low = "Possible duplicate of kid-x (same subject=devops, cosine=0.80)"
        assert rank_pair(None, detail_low) == "red"

    def test_no_cosine_red(self):
        """Без cosine вовсе → 🔴."""
        assert rank_pair({}) == "red"


class TestHelpers:
    """has_negation_pattern, rel_length_diff, extract_target_kid, canonical."""

    def test_negation_antonyms(self):
        assert has_negation_pattern("chto-lyubit-ai", "chto-ne-lyubit-ai") is True
        assert has_negation_pattern("kid-a", "kid-b") is False

    def test_rel_length_diff(self):
        assert rel_length_diff(100, 100) == 0.0
        assert rel_length_diff(100, 200) == 0.5
        assert rel_length_diff(None, 100) is None

    def test_extract_target_kid(self):
        detail = "Possible duplicate of engineering-kb-001 (same subject=devops, cosine=0.95)"
        assert extract_target_kid(detail) == "engineering-kb-001"
        assert extract_target_kid("no pattern") == ""

    def test_recommend_canonical_longer_wins(self):
        assert recommend_canonical(_meta(content_length=500, target_content_length=100),
                                   "src", "tgt") == "src"
        assert recommend_canonical(_meta(content_length=100, target_content_length=500),
                                   "src", "tgt") == "tgt"

    def test_recommend_canonical_standalone_preferred(self):
        meta = _meta(
            content_length=300, target_content_length=300,
            standalone=True, target_standalone=False,
        )
        assert recommend_canonical(meta, "src", "tgt") == "src"

    def test_is_r1_exact_hash_true(self):
        """is_r1_exact_hash: exact hash + standalone + same_subject → True."""
        assert is_r1_exact_hash(_meta(
            content_hash="abc", target_content_hash="abc",
        )) is True

    def test_is_r1_exact_hash_false_on_hash_mismatch(self):
        assert is_r1_exact_hash(_meta(
            content_hash="abc", target_content_hash="xyz",
        )) is False

    def test_is_r1_exact_hash_false_on_non_standalone(self):
        assert is_r1_exact_hash(_meta(
            content_hash="abc", target_content_hash="abc",
            standalone=False,
        )) is False

    def test_is_r1_exact_hash_false_on_different_subject(self):
        assert is_r1_exact_hash(_meta(
            content_hash="abc", target_content_hash="abc",
            target_subject="other",
        )) is False

    def test_is_r1_exact_hash_false_on_legacy_no_target_subject(self):
        assert is_r1_exact_hash(_meta(
            content_hash="abc", target_content_hash="abc",
            target_subject=None,
        )) is False

    def test_summarize_signals(self):
        s = summarize_signals(_meta(cosine=0.974))
        assert s["cosine"] == 0.974
        assert s["hash_match"] is False
        assert s["slug_negation"] is False
