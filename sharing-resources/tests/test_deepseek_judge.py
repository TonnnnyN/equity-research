from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

from market_sentiment.subagent_sentiment import (
    DeepSeekSentimentJudge,
    PostToJudge,
    StubSentimentJudge,
    build_default_sentiment_judge,
)


def make_post(
    post_id: str = "p1",
    ticker: str = "MSFT",
    title: str = "Test post",
    body: str = "Test body",
    source: str = "reddit",
) -> PostToJudge:
    return PostToJudge(
        source=source,
        post_id=post_id,
        ticker=ticker,
        posted_at=datetime(2026, 3, 26, 12, 0, 0, tzinfo=timezone.utc),
        title=title,
        body=body,
        engagement_score=10.0,
    )


def make_deepseek_response(judgements_data: list[dict]) -> bytes:
    """Create an OpenAI-compatible API response."""
    judgements_json = {"judgements": judgements_data}
    response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(judgements_json)
                }
            }
        ]
    }
    return json.dumps(response).encode("utf-8")


class TestDeepSeekSentimentJudge(TestCase):
    def test_single_batch_parsed_correctly(self) -> None:
        """Mock returns standard JSON -> verify SentimentJudgement list is correct, is_stub=False"""
        posts = [
            make_post("p1", "MSFT", "Bullish on Azure", "Cloud demand is strong"),
            make_post("p2", "MSFT", "Worried earnings", "Competition is fierce"),
        ]

        judgements_data = [
            {
                "post_id": "p1",
                "sentiment": "bull",
                "confidence": 0.85,
                "one_line_summary": "Azure 需求强劲，看好云计算前景",
            },
            {
                "post_id": "p2",
                "sentiment": "bear",
                "confidence": 0.7,
                "one_line_summary": "竞争加剧，对收益前景担忧",
            },
        ]

        judge = DeepSeekSentimentJudge(api_key="test-key")

        with patch("market_sentiment.subagent_sentiment.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = make_deepseek_response(judgements_data)
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            judgements = judge.judge_batch(posts)

            self.assertEqual(len(judgements), 2)
            self.assertEqual(judgements[0].post_id, "p1")
            self.assertEqual(judgements[0].sentiment, "bull")
            self.assertEqual(judgements[0].confidence, 0.85)
            self.assertFalse(judgements[0].is_stub)
            self.assertEqual(judgements[1].post_id, "p2")
            self.assertEqual(judgements[1].sentiment, "bear")
            self.assertEqual(judgements[1].confidence, 0.7)

    def test_batch_split_when_over_batch_size(self) -> None:
        """50 posts with batch_size=20 -> API called 3 times"""
        posts = [make_post(f"p{i}", "MSFT") for i in range(50)]

        judge = DeepSeekSentimentJudge(api_key="test-key", batch_size=20)

        call_count = [0]

        def side_effect_func(*args, **kwargs):
            call_count[0] += 1
            # Generate appropriate judgements for the chunk
            judgements_data = [
                {
                    "post_id": post.post_id,
                    "sentiment": "neutral",
                    "confidence": 0.5,
                    "one_line_summary": "中性评论",
                }
                for post in posts
            ]
            mock_response = MagicMock()
            mock_response.read.return_value = make_deepseek_response(judgements_data)
            mock_response.__enter__.return_value = mock_response
            return mock_response

        with patch("market_sentiment.subagent_sentiment.urlopen", side_effect=side_effect_func) as mock_urlopen:
            judgements = judge.judge_batch(posts)

            self.assertEqual(len(judgements), 50)
            # API should be called 3 times (20 + 20 + 10)
            self.assertEqual(mock_urlopen.call_count, 3)

    def test_missing_post_in_response_skipped(self) -> None:
        """Input 5 posts, response only has 4 -> return 4, no error"""
        posts = [make_post(f"p{i}", "MSFT") for i in range(5)]

        # Response only includes judgements for first 4 posts
        judgements_data = [
            {
                "post_id": f"p{i}",
                "sentiment": "neutral",
                "confidence": 0.5,
                "one_line_summary": "中性",
            }
            for i in range(4)
        ]

        judge = DeepSeekSentimentJudge(api_key="test-key")

        with patch("market_sentiment.subagent_sentiment.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = make_deepseek_response(judgements_data)
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            judgements = judge.judge_batch(posts)

            self.assertEqual(len(judgements), 4)

    def test_invalid_json_returns_empty(self) -> None:
        """Mock returns invalid JSON -> return [], no crash"""
        posts = [make_post("p1", "MSFT")]

        judge = DeepSeekSentimentJudge(api_key="test-key", max_retries=1)

        with patch("market_sentiment.subagent_sentiment.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b"not json"
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            judgements = judge.judge_batch(posts)

            self.assertEqual(len(judgements), 0)

    def test_api_error_returns_empty(self) -> None:
        """Mock throws HTTPError -> return [], log warning"""
        from urllib.error import HTTPError

        posts = [make_post("p1", "MSFT")]

        judge = DeepSeekSentimentJudge(api_key="test-key", max_retries=1)

        with patch("market_sentiment.subagent_sentiment.urlopen") as mock_urlopen:
            mock_http_error = HTTPError("http://example.com", 500, "Internal Server Error", {}, None)
            mock_urlopen.side_effect = mock_http_error

            judgements = judge.judge_batch(posts)

            self.assertEqual(len(judgements), 0)

    def test_invalid_sentiment_fallback_neutral(self) -> None:
        """DeepSeek returns sentiment='confused' -> actual value becomes 'neutral'"""
        posts = [make_post("p1", "MSFT")]

        judgements_data = [
            {
                "post_id": "p1",
                "sentiment": "confused",  # invalid sentiment
                "confidence": 0.6,
                "one_line_summary": "摘要",
            }
        ]

        judge = DeepSeekSentimentJudge(api_key="test-key")

        with patch("market_sentiment.subagent_sentiment.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = make_deepseek_response(judgements_data)
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            judgements = judge.judge_batch(posts)

            self.assertEqual(len(judgements), 1)
            self.assertEqual(judgements[0].sentiment, "neutral")

    def test_confidence_clamped(self) -> None:
        """DeepSeek returns confidence=1.5 -> clamp to 1.0"""
        posts = [make_post("p1", "MSFT")]

        judgements_data = [
            {
                "post_id": "p1",
                "sentiment": "bull",
                "confidence": 1.5,  # over 1.0
                "one_line_summary": "看好",
            }
        ]

        judge = DeepSeekSentimentJudge(api_key="test-key")

        with patch("market_sentiment.subagent_sentiment.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = make_deepseek_response(judgements_data)
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            judgements = judge.judge_batch(posts)

            self.assertEqual(len(judgements), 1)
            self.assertEqual(judgements[0].confidence, 1.0)

    def test_confidence_clamped_negative(self) -> None:
        """DeepSeek returns confidence=-0.5 -> clamp to 0.0"""
        posts = [make_post("p1", "MSFT")]

        judgements_data = [
            {
                "post_id": "p1",
                "sentiment": "bear",
                "confidence": -0.5,  # under 0.0
                "one_line_summary": "看跌",
            }
        ]

        judge = DeepSeekSentimentJudge(api_key="test-key")

        with patch("market_sentiment.subagent_sentiment.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = make_deepseek_response(judgements_data)
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            judgements = judge.judge_batch(posts)

            self.assertEqual(len(judgements), 1)
            self.assertEqual(judgements[0].confidence, 0.0)

    def test_build_default_uses_stub_without_env(self) -> None:
        """DEEPSEEK_API_KEY not in env -> return StubSentimentJudge"""
        with patch.dict(os.environ, {}, clear=False):
            if "DEEPSEEK_API_KEY" in os.environ:
                del os.environ["DEEPSEEK_API_KEY"]

            judge = build_default_sentiment_judge()

            self.assertIsInstance(judge, StubSentimentJudge)

    def test_build_default_uses_deepseek_with_env(self) -> None:
        """DEEPSEEK_API_KEY set -> return DeepSeekSentimentJudge"""
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}):
            judge = build_default_sentiment_judge()

            self.assertIsInstance(judge, DeepSeekSentimentJudge)

    def test_empty_posts_returns_empty(self) -> None:
        """judge_batch([]) -> return []"""
        judge = DeepSeekSentimentJudge(api_key="test-key")
        judgements = judge.judge_batch([])
        self.assertEqual(len(judgements), 0)

    def test_direct_array_response_format(self) -> None:
        """Response is direct [] instead of {judgements: []} -> both handled"""
        posts = [make_post("p1", "MSFT")]

        # Response is direct array instead of wrapped in "judgements"
        judgements_data = [
            {
                "post_id": "p1",
                "sentiment": "bull",
                "confidence": 0.75,
                "one_line_summary": "看好",
            }
        ]

        judge = DeepSeekSentimentJudge(api_key="test-key")

        with patch("market_sentiment.subagent_sentiment.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            # Directly return array format instead of wrapped
            direct_response = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(judgements_data)
                        }
                    }
                ]
            }
            mock_response.read.return_value = json.dumps(direct_response).encode("utf-8")
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            judgements = judge.judge_batch(posts)

            self.assertEqual(len(judgements), 1)
            self.assertEqual(judgements[0].sentiment, "bull")

    def test_missing_confidence_defaults_to_half(self) -> None:
        """Judgment missing confidence field -> default to 0.5"""
        posts = [make_post("p1", "MSFT")]

        judgements_data = [
            {
                "post_id": "p1",
                "sentiment": "bull",
                # missing confidence
                "one_line_summary": "看好",
            }
        ]

        judge = DeepSeekSentimentJudge(api_key="test-key")

        with patch("market_sentiment.subagent_sentiment.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = make_deepseek_response(judgements_data)
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            judgements = judge.judge_batch(posts)

            self.assertEqual(len(judgements), 1)
            self.assertEqual(judgements[0].confidence, 0.5)

    def test_missing_summary_uses_title(self) -> None:
        """Judgment missing one_line_summary -> use post title[:80]"""
        posts = [make_post("p1", "MSFT", title="My Title")]

        judgements_data = [
            {
                "post_id": "p1",
                "sentiment": "neutral",
                "confidence": 0.5,
                # missing one_line_summary
            }
        ]

        judge = DeepSeekSentimentJudge(api_key="test-key")

        with patch("market_sentiment.subagent_sentiment.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = make_deepseek_response(judgements_data)
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            judgements = judge.judge_batch(posts)

            self.assertEqual(len(judgements), 1)
            self.assertEqual(judgements[0].one_line_summary, "My Title")

    def test_multiple_api_calls_in_batch(self) -> None:
        """Large batch triggers multiple API calls, all results merged"""
        # Create 45 posts to test splitting with batch_size=20
        posts = [make_post(f"p{i}", "MSFT") for i in range(45)]

        def make_response_for_chunk(chunk_posts):
            """Return OpenAI-compatible response for a chunk"""
            judgements_data = [
                {
                    "post_id": post.post_id,
                    "sentiment": "neutral",
                    "confidence": 0.5,
                    "one_line_summary": f"summary_{post.post_id}",
                }
                for post in chunk_posts
            ]
            return make_deepseek_response(judgements_data)

        judge = DeepSeekSentimentJudge(api_key="test-key", batch_size=20)

        call_count = [0]

        def side_effect_func(*args, **kwargs):
            call_count[0] += 1
            mock_response = MagicMock()
            # Return response with 20, 20, and 5 posts respectively
            if call_count[0] == 1:
                mock_response.read.return_value = make_response_for_chunk(posts[:20])
            elif call_count[0] == 2:
                mock_response.read.return_value = make_response_for_chunk(posts[20:40])
            else:
                mock_response.read.return_value = make_response_for_chunk(posts[40:45])
            mock_response.__enter__.return_value = mock_response
            return mock_response

        with patch("market_sentiment.subagent_sentiment.urlopen", side_effect=side_effect_func) as mock_urlopen:
            judgements = judge.judge_batch(posts)

            self.assertEqual(len(judgements), 45)
            self.assertEqual(mock_urlopen.call_count, 3)
