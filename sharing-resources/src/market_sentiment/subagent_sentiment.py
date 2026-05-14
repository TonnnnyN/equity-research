from __future__ import annotations

import certifi
import json
import logging
import os
import ssl
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PostToJudge:
    source: str
    post_id: str
    ticker: str
    posted_at: datetime
    title: str
    body: str
    engagement_score: float


@dataclass(slots=True)
class SentimentJudgement:
    source: str
    post_id: str
    ticker: str
    sentiment: str
    confidence: float
    one_line_summary: str
    is_stub: bool = False


class SentimentJudge(Protocol):
    def judge_batch(self, posts: list[PostToJudge]) -> list[SentimentJudgement]:
        ...


class StubSentimentJudge:
    """Stub implementation: returns neutral for all posts.

    Real Haiku subagent integration is P0 step 3.
    TODO: Replace with actual Anthropic API call in next step.
    """

    def judge_batch(self, posts: list[PostToJudge]) -> list[SentimentJudgement]:
        return [
            SentimentJudgement(
                source=p.source,
                post_id=p.post_id,
                ticker=p.ticker,
                sentiment="neutral",
                confidence=0.5,
                one_line_summary=p.title[:120],
                is_stub=True,
            )
            for p in posts
        ]


class DeepSeekSentimentJudge:
    """
    用 DeepSeek API 读帖判情绪。
    一次最多 batch_size 个帖子合并成一次 API 调用,返回 JSON 数组。

    失败处理:
    - 网络/API 错误 → log warning, 返回 []
    - JSON 解析失败 → log warning, 返回 []
    - 部分帖子在响应里漏了 → 漏的那些不出现在返回列表里(上层 social_service 会兜底走 unknown 路径)
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "deepseek-v4-flash",
        base_url: str = "https://api.deepseek.com",
        batch_size: int = 20,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._ssl_context = ssl.create_default_context(cafile=certifi.where())

    def judge_batch(self, posts: list[PostToJudge]) -> list[SentimentJudgement]:
        """Judge a batch of posts, splitting into chunks if needed."""
        if not posts:
            return []

        all_judgements = []
        for i in range(0, len(posts), self.batch_size):
            chunk = posts[i : i + self.batch_size]
            chunk_judgements = self._judge_chunk(chunk)
            all_judgements.extend(chunk_judgements)

        return all_judgements

    def _judge_chunk(self, posts: list[PostToJudge]) -> list[SentimentJudgement]:
        """Judge a single chunk of posts (up to batch_size)."""
        if not posts:
            return []

        ticker = posts[0].ticker if posts else "UNKNOWN"
        prompt = self._build_prompt(ticker, posts)

        for attempt in range(self.max_retries):
            try:
                response_text = self._call_deepseek_api(prompt)
                if not response_text:
                    raise RuntimeError("Empty response from DeepSeek API")
                judgements = self._parse_response(response_text, posts)
                return judgements
            except Exception as exc:
                logger.warning(
                    f"DeepSeek judgment attempt {attempt + 1}/{self.max_retries} failed for "
                    f"{len(posts)} posts (ticker={ticker}): {exc}"
                )
                if attempt == self.max_retries - 1:
                    logger.warning(f"Giving up on judgment for {len(posts)} posts after {self.max_retries} retries")
                    return []

        return []

    def _build_prompt(self, ticker: str, posts: list[PostToJudge]) -> str:
        """Build the prompt for DeepSeek."""
        posts_text = ""
        for idx, post in enumerate(posts, 1):
            posts_text += f"[{idx}] post_id={post.post_id}, title={post.title}\n"
            posts_text += f"正文: {post.body}\n\n"

        system_message = (
            "你是金融帖子情绪分类器。读取一批关于美股的社交媒体帖子,对每个帖子输出:\n"
            "- sentiment: \"bull\"(看涨/积极) / \"bear\"(看跌/担忧/恐慌) / \"neutral\"(中性/无明确方向)\n"
            "- confidence: 0.0-1.0 浮点数\n"
            "- one_line_summary: ≤80 字的中文摘要,概括帖子主要信息\n\n"
            "输出严格 JSON 格式:{\"judgements\": [{\"post_id\": \"...\", \"sentiment\": \"...\", \"confidence\": 0.X, \"one_line_summary\": \"...\"}, ...]}\n"
            "post_id 必须与输入完全一致。"
        )

        user_message = f"股票代码: {ticker}\n帖子列表:\n{posts_text}"

        return json.dumps(
            {
                "system": system_message,
                "user": user_message,
            }
        )

    def _call_deepseek_api(self, prompt: str) -> str:
        """Call DeepSeek API and return response text."""
        prompt_dict = json.loads(prompt)
        system_msg = prompt_dict["system"]
        user_msg = prompt_dict["user"]

        request_body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        request = Request(
            url,
            data=json.dumps(request_body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds, context=self._ssl_context) as response:
                response_body = response.read().decode("utf-8")
                response_json = json.loads(response_body)
                # Extract the assistant message
                if "choices" in response_json and response_json["choices"]:
                    return response_json["choices"][0]["message"]["content"]
                else:
                    raise RuntimeError(f"Unexpected DeepSeek response structure: {response_json}")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {exc.code} from DeepSeek: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Network error calling DeepSeek: {exc.reason}") from exc

    def _parse_response(self, response_text: str, posts: list[PostToJudge]) -> list[SentimentJudgement]:
        """Parse DeepSeek JSON response and return SentimentJudgement list."""
        try:
            response_data = json.loads(response_text)
        except json.JSONDecodeError as exc:
            logger.warning(f"Failed to parse DeepSeek response as JSON: {exc}")
            return []

        # Handle both {"judgements": [...]} and [...] formats
        judgements_list = response_data
        if isinstance(response_data, dict) and "judgements" in response_data:
            judgements_list = response_data["judgements"]

        if not isinstance(judgements_list, list):
            logger.warning(f"Expected judgements to be a list, got {type(judgements_list)}")
            return []

        # Map by post_id for O(1) lookup
        posts_by_id = {p.post_id: p for p in posts}

        result = []
        for item in judgements_list:
            if not isinstance(item, dict):
                logger.debug(f"Skipping non-dict judgment item: {item}")
                continue

            post_id = item.get("post_id")
            if not post_id or post_id not in posts_by_id:
                logger.debug(f"Skipping judgment with unknown post_id: {post_id}")
                continue

            original_post = posts_by_id[post_id]
            sentiment = item.get("sentiment", "neutral")
            if sentiment not in ("bull", "bear", "neutral"):
                sentiment = "neutral"

            confidence = item.get("confidence", 0.5)
            if not isinstance(confidence, (int, float)):
                confidence = 0.5
            confidence = max(0.0, min(1.0, float(confidence)))

            one_line_summary = str(item.get("one_line_summary", original_post.title[:80]))[:80]

            result.append(
                SentimentJudgement(
                    source=original_post.source,
                    post_id=post_id,
                    ticker=original_post.ticker,
                    sentiment=sentiment,
                    confidence=confidence,
                    one_line_summary=one_line_summary,
                    is_stub=False,
                )
            )

        return result


def build_default_sentiment_judge() -> SentimentJudge:
    """Factory function to build sentiment judge based on environment."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        logger.warning("DEEPSEEK_API_KEY not set, falling back to StubSentimentJudge")
        return StubSentimentJudge()
    return DeepSeekSentimentJudge(api_key=api_key)
