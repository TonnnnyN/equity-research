from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from market_sentiment.config import ProjectConfig
from market_sentiment.sources.x_provider import _provider_candidates


@dataclass(slots=True)
class PreflightCheck:
    name: str
    ok: bool
    message: str
    blocking: bool = True


@dataclass(slots=True)
class PreflightSummary:
    checks: list[PreflightCheck]

    @property
    def ready(self) -> bool:
        return all(check.ok or not check.blocking for check in self.checks)

    def to_lines(self) -> list[str]:
        lines = [f"Preflight: {'READY' if self.ready else 'BLOCKED'}"]
        for check in self.checks:
            state = "OK" if check.ok else ("WARN" if not check.blocking else "FAIL")
            lines.append(f"- [{state}] {check.name}: {check.message}")
        return lines


def _build_api_key_checks(config: ProjectConfig) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    api_keys = [
        ("ALPHAVANTAGE_API_KEY", True),
        ("FRED_API_KEY", True),
        ("EIA_API_KEY", False),
        ("SEC_USER_AGENT", True),
        ("DEEPSEEK_API_KEY", True),
    ]

    for key_name, blocking in api_keys:
        key_value = os.environ.get(key_name)
        if key_value and len(key_value) > 5:
            checks.append(
                PreflightCheck(
                    f"API key: {key_name}",
                    True,
                    f"configured (length {len(key_value)})",
                    blocking=blocking,
                )
            )
        elif key_value:
            checks.append(
                PreflightCheck(
                    f"API key: {key_name}",
                    False,
                    "set but empty",
                    blocking=blocking,
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    f"API key: {key_name}",
                    False,
                    "missing — set in secrets file",
                    blocking=blocking,
                )
            )

    if config.social.reddit.enabled:
        for reddit_key in ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"]:
            key_value = os.environ.get(reddit_key)
            if key_value and len(key_value) > 5:
                checks.append(
                    PreflightCheck(
                        f"API key: {reddit_key}",
                        True,
                        f"configured (length {len(key_value)})",
                        blocking=False,
                    )
                )
            elif key_value:
                checks.append(
                    PreflightCheck(
                        f"API key: {reddit_key}",
                        False,
                        "set but empty",
                        blocking=False,
                    )
                )
            else:
                checks.append(
                    PreflightCheck(
                        f"API key: {reddit_key}",
                        False,
                        "missing — set in secrets file",
                        blocking=False,
                    )
                )

    return checks


def _build_social_provider_check(config: ProjectConfig) -> list[PreflightCheck]:
    if not config.social.enabled:
        return []

    reddit_usable = config.social.reddit.enabled
    x_usable = config.social.x.enabled
    forum_usable = config.social.forum.enabled and bool(config.social.forum.base_urls)

    if not reddit_usable and not x_usable and not forum_usable:
        return [
            PreflightCheck(
                "Social providers",
                False,
                "social.enabled=True but reddit/forum/x all unusable — social_rebound will silently be 0",
                blocking=False,
            )
        ]

    provider_list = []
    if reddit_usable:
        provider_list.append("reddit on")
    else:
        provider_list.append("reddit off")
    if x_usable:
        provider_list.append("x on")
    else:
        provider_list.append("x off")
    if forum_usable:
        provider_list.append("forum on")
    else:
        provider_list.append("forum off")

    return [
        PreflightCheck(
            "Social providers",
            True,
            f"at least one provider configured ({', '.join(provider_list)})",
            blocking=False,
        )
    ]


def build_preflight_summary(config: ProjectConfig) -> PreflightSummary:
    checks: list[PreflightCheck] = []
    checks.extend(_build_api_key_checks(config))
    checks.extend(_build_x_checks(config))
    checks.extend(_build_social_provider_check(config))
    checks.extend(_build_options_checks(config))
    return PreflightSummary(checks=checks)


def _build_x_checks(config: ProjectConfig) -> list[PreflightCheck]:
    if not config.social.enabled or not config.social.x.enabled:
        return [PreflightCheck("X runtime", True, "disabled in current config", blocking=False)]

    candidates = _provider_candidates(config.social.x.provider)
    usable_paths: list[str] = []
    checks: list[PreflightCheck] = []

    if "twscrape" in candidates:
        db_path = config.social.x.db_path
        accounts_file = config.social.x.accounts_file
        db_exists = bool(db_path and Path(db_path).exists())
        bootstrap_ready = bool(db_path and accounts_file and Path(accounts_file).exists())
        if db_exists:
            usable_paths.append("twscrape")
            checks.append(
                PreflightCheck(
                    "X twscrape",
                    True,
                    f"accounts db ready at {db_path}",
                    blocking=False,
                )
            )
        elif bootstrap_ready:
            usable_paths.append("twscrape")
            checks.append(
                PreflightCheck(
                    "X twscrape",
                    True,
                    f"accounts bootstrap ready from {accounts_file}",
                    blocking=False,
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    "X twscrape",
                    False,
                    "need an existing X_DB_PATH accounts db or X_ACCOUNTS_FILE + X_DB_PATH for bootstrap",
                    blocking=False,
                )
            )

    if "twikit" in candidates:
        cookies_path = config.social.x.cookies_path
        cookies_ready = bool(cookies_path and Path(cookies_path).exists())
        login_ready = bool(config.social.x.username and config.social.x.password)
        if cookies_ready:
            usable_paths.append("twikit")
            checks.append(
                PreflightCheck(
                    "X twikit",
                    True,
                    f"cookies ready at {cookies_path}",
                    blocking=False,
                )
            )
        elif login_ready:
            usable_paths.append("twikit")
            checks.append(
                PreflightCheck(
                    "X twikit",
                    True,
                    "login credentials configured",
                    blocking=False,
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    "X twikit",
                    False,
                    "need X_COOKIES_PATH or X_USERNAME + X_PASSWORD",
                    blocking=False,
                )
            )

    if usable_paths:
        checks.insert(
            0,
            PreflightCheck(
                "X runtime",
                True,
                f"usable X credential/config path found via {', '.join(usable_paths)}",
            ),
        )
    else:
        checks.insert(
            0,
            PreflightCheck(
                "X runtime",
                False,
                "no usable X credential/config path found; configure twscrape db/bootstrap or twikit cookies/login",
            ),
        )
    return checks


def _build_options_checks(config: ProjectConfig) -> list[PreflightCheck]:
    if not config.options.enabled:
        return [PreflightCheck("Options runtime", True, "disabled in current config", blocking=False)]

    checks: list[PreflightCheck] = []
    if config.options.provider != "alpha_vantage":
        checks.append(
            PreflightCheck(
                "Options runtime",
                False,
                f"unsupported options provider: {config.options.provider}",
            )
        )
        return checks

    api_key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if api_key:
        checks.append(
            PreflightCheck(
                "Options runtime",
                True,
                "ALPHAVANTAGE_API_KEY is configured",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                "Options runtime",
                False,
                "ALPHAVANTAGE_API_KEY is not set",
            )
        )
    checks.append(
        PreflightCheck(
            "Options source note",
            True,
            "Alpha Vantage options endpoints are premium-gated and may degrade to partial coverage",
            blocking=False,
        )
    )
    return checks
