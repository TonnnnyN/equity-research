from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from equity_research.models import Benchmark, Layer, Threshold
from equity_research.sources.social_base import SOCIAL_PROVIDER_NAMES


DEFAULT_CONFIG_PATH = Path("config/default.toml")
DEFAULT_REPORT_RECIPIENT = "1786146194@qq.com"

# Target depth of daily price history to backfill once and retain thereafter, in
# calendar days. 5 years mirrors Yahoo's own "Beta (5Y Monthly)" convention and covers
# the ~20 quarters of SEC fundamentals history the valuation layer holds, so
# own_history_percentile can build a quarter-end multiple series across (up to) the
# same span instead of being capped by a few months of price bars. Deepening this
# number only helps once RetentionConfig.daily_price_days (below) is at least as large
# — a shorter retention window would prune the backfilled history right back out.
PRICE_HISTORY_TARGET_DAYS = 5 * 365  # ~1825 calendar days

# Retention keeps a buffer beyond the target depth so a ticker that was deep-backfilled
# a few weeks ago isn't immediately pruned back to shallow before its next refresh.
_PRICE_HISTORY_RETENTION_BUFFER_DAYS = 90
DEFAULT_DAILY_PRICE_RETENTION_DAYS = PRICE_HISTORY_TARGET_DAYS + _PRICE_HISTORY_RETENTION_BUFFER_DAYS


@dataclass(slots=True)
class EmailDeliveryConfig:
    smtp_host: str = ""
    smtp_port: int = 587
    username: str | None = None
    password: str | None = None
    from_address: str | None = None
    recipients: list[str] = field(default_factory=lambda: [DEFAULT_REPORT_RECIPIENT])
    use_ssl: bool = False
    use_starttls: bool = True
    timeout_seconds: float = 20.0
    subject_prefix: str = "Market Sentiment Report"

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.recipients)


@dataclass(slots=True)
class RetentionConfig:
    report_days: int = 90
    raw_payload_days: int = 30
    social_raw_payload_days: int = 30
    daily_price_days: int = DEFAULT_DAILY_PRICE_RETENTION_DAYS
    official_event_days: int = 365
    fundamental_days: int = 730
    macro_days: int = 365
    run_metadata_days: int = 90
    social_post_days: int = 90
    social_snapshot_days: int = 180


@dataclass(slots=True)
class RedditConfig:
    enabled: bool = True
    subreddits: list[str] = field(
        default_factory=lambda: [
            "stocks",
            "investing",
            "wallstreetbets",
            "options",
            "SecurityAnalysis",
        ]
    )
    max_posts_per_subreddit: int = 75
    user_agent: str = "equity-research-social/0.1"
    client_id: str | None = None
    client_secret: str | None = None


@dataclass(slots=True)
class ForumConfig:
    enabled: bool = False
    base_urls: list[str] = field(default_factory=list)
    max_posts_per_forum: int = 50
    api_key: str | None = None
    api_username: str | None = None


@dataclass(slots=True)
class XConfig:
    enabled: bool = False
    provider: str = "twscrape"
    search_product: str = "Latest"
    max_posts: int = 50
    username: str | None = None
    email: str | None = None
    password: str | None = None
    email_password: str | None = None
    cookies_path: str | None = None
    accounts_file: str | None = None
    accounts_line_format: str = "username:password:email:email_password:_:cookies"
    proxy_url: str | None = None
    db_path: str | None = None


@dataclass(slots=True)
class SocialConfig:
    enabled: bool = False
    providers: list[str] = field(default_factory=lambda: list(SOCIAL_PROVIDER_NAMES))
    provider_timeout_seconds: float = 20.0
    lookback_hours: int = 168
    baseline_days: int = 14
    max_posts_per_source: int = 150
    min_recent_posts: int = 6
    min_unique_authors: int = 5
    min_platform_count: int = 1
    max_author_share: float = 0.40
    max_backfill_days: int = 21
    reddit: RedditConfig = field(default_factory=RedditConfig)
    forum: ForumConfig = field(default_factory=ForumConfig)
    x: XConfig = field(default_factory=XConfig)


@dataclass(slots=True)
class ProjectConfig:
    name: str
    timezone: str
    default_user_agent: str
    data_dir: Path
    db_path: Path
    report_email: EmailDeliveryConfig
    retention: RetentionConfig
    social: SocialConfig
    securities: list[Security]
    benchmarks: dict[str, Benchmark]
    thresholds: dict[Layer, Threshold]


def load_config(config_path: str | None = None) -> ProjectConfig:
    config_override = config_path or os.environ.get("EQUITY_RESEARCH_CONFIG")
    path = Path(config_override) if config_override else DEFAULT_CONFIG_PATH
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    project = raw["project"]
    default_data_dir = project.get("data_dir")
    if not default_data_dir:
        raw_dir = Path(project.get("raw_data_dir", "data/raw"))
        default_data_dir = str(raw_dir.parent)
    data_dir = Path(os.environ.get("EQUITY_RESEARCH_DATA_DIR", default_data_dir))
    db_path = Path(os.environ.get("EQUITY_RESEARCH_DB_PATH", project["db_path"]))

    threshold_block = raw.get("thresholds", raw.get("triggers", {}))
    thresholds = {}
    for layer_name, settings in threshold_block.items():
        if "ten_day_drawdown" in settings:
            thresholds[Layer(layer_name)] = Threshold(**settings)
        else:
            thresholds[Layer(layer_name)] = Threshold(
                ten_day_drawdown=abs(settings["drawdown_10d"]),
                twenty_day_drawdown=abs(settings["drawdown_20d"]),
                relative_underperformance=abs(settings["relative_20d"]),
            )
    benchmarks = {
        benchmark["ticker"]: Benchmark(ticker=benchmark["ticker"], name=benchmark["name"])
        for benchmark in raw.get("benchmarks", [])
    }
    # Securities are no longer loaded from config; all tickers are provided ad-hoc via CLI
    securities = []

    email_block = raw.get("email", {})
    if not isinstance(email_block, dict):
        email_block = {}
    report_email = _load_email_config(email_block)
    retention_block = raw.get("retention", {})
    if not isinstance(retention_block, dict):
        retention_block = {}
    retention = _load_retention_config(retention_block)
    social_block = raw.get("social", {})
    if not isinstance(social_block, dict):
        social_block = {}
    social = _load_social_config(social_block)

    return ProjectConfig(
        name=project.get("name", "equity-research-v1"),
        timezone=project.get("timezone", "America/New_York"),
        default_user_agent=project.get(
            "default_user_agent",
            "equity-research-pipeline/0.1 your-name your-email@example.com",
        ),
        data_dir=data_dir,
        db_path=db_path,
        report_email=report_email,
        retention=retention,
        social=social,
        securities=securities,
        benchmarks=benchmarks,
        thresholds=thresholds,
    )


def _load_email_config(email_block: dict) -> EmailDeliveryConfig:
    smtp_host = _first_non_empty(
        os.environ.get("EQUITY_RESEARCH_SMTP_HOST"),
        email_block.get("smtp_host"),
    )
    smtp_port = _parse_int(
        _first_non_empty(
            os.environ.get("EQUITY_RESEARCH_SMTP_PORT"),
            email_block.get("smtp_port"),
        ),
        default=587,
    )
    username = _first_non_empty(
        os.environ.get("EQUITY_RESEARCH_SMTP_USERNAME"),
        email_block.get("smtp_username"),
    )
    password = _first_non_empty(
        os.environ.get("EQUITY_RESEARCH_SMTP_PASSWORD"),
        email_block.get("smtp_password"),
    )
    from_address = _first_non_empty(
        os.environ.get("EQUITY_RESEARCH_EMAIL_FROM"),
        email_block.get("from_address"),
    )
    recipients = _parse_recipients(
        os.environ.get("EQUITY_RESEARCH_REPORT_EMAIL_TO")
        or email_block.get("recipients")
        or email_block.get("recipient")
        or [DEFAULT_REPORT_RECIPIENT]
    )
    use_ssl = _parse_bool(
        _first_non_empty(
            os.environ.get("EQUITY_RESEARCH_SMTP_USE_SSL"),
            email_block.get("use_ssl"),
        ),
        default=False,
    )
    use_starttls = _parse_bool(
        _first_non_empty(
            os.environ.get("EQUITY_RESEARCH_SMTP_USE_STARTTLS"),
            email_block.get("use_starttls"),
        ),
        default=True,
    )
    timeout_seconds = float(
        _first_non_empty(
            os.environ.get("EQUITY_RESEARCH_SMTP_TIMEOUT_SECONDS"),
            email_block.get("timeout_seconds"),
            20.0,
        )
    )
    subject_prefix = str(
        _first_non_empty(
            os.environ.get("EQUITY_RESEARCH_EMAIL_SUBJECT_PREFIX"),
            email_block.get("subject_prefix"),
            "Market Sentiment Report",
        )
    )

    return EmailDeliveryConfig(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        username=username,
        password=password,
        from_address=from_address,
        recipients=recipients,
        use_ssl=use_ssl,
        use_starttls=use_starttls,
        timeout_seconds=timeout_seconds,
        subject_prefix=subject_prefix,
    )


def _load_retention_config(retention_block: dict) -> RetentionConfig:
    raw_payload_days = _parse_non_negative_int(retention_block.get("raw_payload_days"), 30)
    return RetentionConfig(
        report_days=_parse_non_negative_int(retention_block.get("report_days"), 90),
        raw_payload_days=raw_payload_days,
        social_raw_payload_days=_parse_non_negative_int(retention_block.get("social_raw_payload_days"), raw_payload_days),
        daily_price_days=_parse_non_negative_int(
            retention_block.get("daily_price_days"), DEFAULT_DAILY_PRICE_RETENTION_DAYS
        ),
        official_event_days=_parse_non_negative_int(retention_block.get("official_event_days"), 365),
        fundamental_days=_parse_non_negative_int(retention_block.get("fundamental_days"), 730),
        macro_days=_parse_non_negative_int(retention_block.get("macro_days"), 365),
        run_metadata_days=_parse_non_negative_int(retention_block.get("run_metadata_days"), 90),
        social_post_days=_parse_non_negative_int(retention_block.get("social_post_days"), 90),
        social_snapshot_days=_parse_non_negative_int(retention_block.get("social_snapshot_days"), 180),
    )


def _load_social_config(social_block: dict) -> SocialConfig:
    providers = social_block.get("providers", list(SOCIAL_PROVIDER_NAMES))
    if isinstance(providers, str):
        providers = [providers]
    if not isinstance(providers, list):
        providers = list(SOCIAL_PROVIDER_NAMES)
    reddit_block = social_block.get("reddit", {})
    if not isinstance(reddit_block, dict):
        reddit_block = {}
    subreddit_values = (
        os.environ.get("REDDIT_SUBREDDITS")
        or reddit_block.get("subreddits")
        or social_block.get("subreddits")
        or RedditConfig().subreddits
    )
    if isinstance(subreddit_values, str):
        subreddit_list = [item.strip() for item in subreddit_values.split(",") if item.strip()]
    elif isinstance(subreddit_values, list):
        subreddit_list = [str(item).strip() for item in subreddit_values if str(item).strip()]
    else:
        subreddit_list = RedditConfig().subreddits
    forum_block = social_block.get("forum", {})
    if not isinstance(forum_block, dict):
        forum_block = {}
    forum_base_urls = _parse_string_list(
        os.environ.get("FORUM_BASE_URLS")
        or forum_block.get("base_urls")
        or []
    )
    x_block = social_block.get("x", {})
    if not isinstance(x_block, dict):
        x_block = {}
    reddit = RedditConfig(
        enabled=_parse_bool(
            _first_non_empty(
                os.environ.get("REDDIT_ENABLED"),
                reddit_block.get("enabled"),
                "reddit" in providers,
            ),
            default="reddit" in providers,
        ),
        subreddits=subreddit_list or RedditConfig().subreddits,
        max_posts_per_subreddit=_parse_non_negative_int(
            _first_non_empty(
                os.environ.get("REDDIT_MAX_POSTS_PER_SUBREDDIT"),
                reddit_block.get("max_posts_per_subreddit"),
                social_block.get("max_posts_per_subreddit"),
            ),
            75,
        ),
        user_agent=str(
            _first_non_empty(
                os.environ.get("REDDIT_USER_AGENT"),
                reddit_block.get("user_agent"),
                "equity-research-social/0.1",
            )
        ),
        client_id=_first_non_empty(os.environ.get("REDDIT_CLIENT_ID"), reddit_block.get("client_id")),
        client_secret=_first_non_empty(
            os.environ.get("REDDIT_CLIENT_SECRET"),
            reddit_block.get("client_secret"),
        ),
    )
    forum = ForumConfig(
        enabled=_parse_bool(
            _first_non_empty(
                os.environ.get("FORUM_ENABLED"),
                forum_block.get("enabled"),
                "forum" in providers,
            ),
            default="forum" in providers,
        ),
        base_urls=forum_base_urls,
        max_posts_per_forum=_parse_non_negative_int(
            _first_non_empty(
                os.environ.get("FORUM_MAX_POSTS_PER_FORUM"),
                forum_block.get("max_posts_per_forum"),
                50,
            ),
            50,
        ),
        api_key=_first_non_empty(os.environ.get("FORUM_API_KEY"), forum_block.get("api_key")),
        api_username=_first_non_empty(
            os.environ.get("FORUM_API_USERNAME"),
            forum_block.get("api_username"),
        ),
    )
    x_config = XConfig(
        enabled=_parse_bool(
            _first_non_empty(
                os.environ.get("X_ENABLED"),
                x_block.get("enabled"),
                "x" in providers,
            ),
            default="x" in providers,
        ),
        provider=str(_first_non_empty(os.environ.get("X_PROVIDER"), x_block.get("provider"), "twscrape")),
        search_product=_normalize_x_search_product(
            str(_first_non_empty(os.environ.get("X_SEARCH_PRODUCT"), x_block.get("search_product"), "Latest"))
        ),
        max_posts=_parse_non_negative_int(
            _first_non_empty(os.environ.get("X_MAX_POSTS"), x_block.get("max_posts")),
            50,
        ),
        username=_first_non_empty(os.environ.get("X_USERNAME"), x_block.get("username")),
        email=_first_non_empty(os.environ.get("X_EMAIL"), x_block.get("email")),
        password=_first_non_empty(os.environ.get("X_PASSWORD"), x_block.get("password")),
        email_password=_first_non_empty(os.environ.get("X_EMAIL_PASSWORD"), x_block.get("email_password")),
        cookies_path=_first_non_empty(os.environ.get("X_COOKIES_PATH"), x_block.get("cookies_path")),
        accounts_file=_first_non_empty(os.environ.get("X_ACCOUNTS_FILE"), x_block.get("accounts_file")),
        accounts_line_format=str(
            _first_non_empty(
                os.environ.get("X_ACCOUNTS_LINE_FORMAT"),
                x_block.get("accounts_line_format"),
                XConfig().accounts_line_format,
            )
        ),
        proxy_url=_first_non_empty(os.environ.get("X_PROXY_URL"), x_block.get("proxy_url")),
        db_path=_first_non_empty(os.environ.get("X_DB_PATH"), x_block.get("db_path")),
    )
    return SocialConfig(
        enabled=_parse_bool(_first_non_empty(os.environ.get("SOCIAL_ENABLED"), social_block.get("enabled")), default=False),
        providers=[str(item).strip() for item in providers if str(item).strip()] or list(SOCIAL_PROVIDER_NAMES),
        provider_timeout_seconds=_parse_non_negative_float(
            _first_non_empty(os.environ.get("SOCIAL_PROVIDER_TIMEOUT_SECONDS"), social_block.get("provider_timeout_seconds")),
            20.0,
        ),
        lookback_hours=_parse_non_negative_int(
            _first_non_empty(os.environ.get("SOCIAL_LOOKBACK_HOURS"), social_block.get("lookback_hours")),
            168,
        ),
        baseline_days=_parse_non_negative_int(
            _first_non_empty(os.environ.get("SOCIAL_BASELINE_DAYS"), social_block.get("baseline_days")),
            14,
        ),
        max_posts_per_source=_parse_non_negative_int(
            _first_non_empty(os.environ.get("SOCIAL_MAX_POSTS_PER_SOURCE"), social_block.get("max_posts_per_source")),
            150,
        ),
        min_recent_posts=_parse_non_negative_int(
            _first_non_empty(os.environ.get("SOCIAL_MIN_RECENT_POSTS"), social_block.get("min_recent_posts")),
            6,
        ),
        min_unique_authors=_parse_non_negative_int(
            _first_non_empty(os.environ.get("SOCIAL_MIN_UNIQUE_AUTHORS"), social_block.get("min_unique_authors")),
            5,
        ),
        min_platform_count=_parse_non_negative_int(
            _first_non_empty(os.environ.get("SOCIAL_MIN_PLATFORM_COUNT"), social_block.get("min_platform_count")),
            1,
        ),
        max_author_share=float(
            _first_non_empty(os.environ.get("SOCIAL_MAX_AUTHOR_SHARE"), social_block.get("max_author_share"), 0.40)
        ),
        max_backfill_days=_parse_non_negative_int(
            _first_non_empty(os.environ.get("SOCIAL_MAX_BACKFILL_DAYS"), social_block.get("max_backfill_days")),
            21,
        ),
        reddit=reddit,
        forum=forum,
        x=x_config,
    )


def _first_non_empty(*values: object) -> object:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _parse_int(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return int(value)
    return default


def _parse_non_negative_int(value: object, default: int) -> int:
    return max(0, _parse_int(value, default))


def _parse_non_negative_float(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str) and value.strip():
        return max(0.0, float(value))
    return default


def _parse_recipients(value: object) -> list[str]:
    if value is None:
        return [DEFAULT_REPORT_RECIPIENT]
    if isinstance(value, str):
        recipients = [item.strip() for item in value.split(",") if item.strip()]
        return recipients or [DEFAULT_REPORT_RECIPIENT]
    if isinstance(value, list):
        recipients = [str(item).strip() for item in value if str(item).strip()]
        return recipients or [DEFAULT_REPORT_RECIPIENT]
    return [str(value).strip()] if str(value).strip() else [DEFAULT_REPORT_RECIPIENT]


def _parse_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    normalized = str(value).strip()
    return [normalized] if normalized else []


def _normalize_x_search_product(value: str) -> str:
    normalized = value.strip().lower()
    mapping = {
        "top": "Top",
        "latest": "Latest",
        "media": "Media",
    }
    return mapping.get(normalized, "Latest")
