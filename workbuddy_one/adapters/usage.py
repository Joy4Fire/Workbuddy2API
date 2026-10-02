"""协议用量归一化：保留积分和上游明示的缓存、思考 token，零值也有效。"""
from __future__ import annotations


def normalize_chat_usage(usage: dict | None) -> dict:
    if not usage:
        return {}
    result = dict(usage)
    result['prompt_tokens'] = usage.get('prompt_tokens', usage.get('input_tokens', 0))
    result['completion_tokens'] = usage.get('completion_tokens', usage.get('output_tokens', 0))
    return result


def _detail(usage: dict, nested_keys: tuple[str, ...], field: str, flat_keys: tuple[str, ...]) -> int:
    for key in nested_keys:
        details = usage.get(key)
        if isinstance(details, dict) and details.get(field) is not None:
            return details[field]
    for key in flat_keys:
        if usage.get(key) is not None:
            return usage[key]
    return 0


def responses_usage(usage: dict | None) -> dict | None:
    if not usage:
        return None
    normalized = normalize_chat_usage(usage)
    input_tokens = normalized['prompt_tokens']
    output_tokens = normalized['completion_tokens']
    return {
        'input_tokens': input_tokens,
        'input_tokens_details': {'cached_tokens': _detail(
            usage, ('prompt_tokens_details', 'input_tokens_details'), 'cached_tokens',
            ('prompt_cache_hit_tokens', 'cache_read_tokens', 'cache_read_input_tokens', 'cached_tokens'))},
        'output_tokens': output_tokens,
        'output_tokens_details': {'reasoning_tokens': _detail(
            usage, ('completion_tokens_details', 'output_tokens_details'), 'reasoning_tokens', ('reasoning_tokens',))},
        'total_tokens': usage.get('total_tokens', input_tokens + output_tokens),
    }
