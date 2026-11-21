"""Shared, advisory length guidance; this module does not validate output."""
import re


TEXT_CONSTRAINT_GUIDANCE = (
    '用户提出明确字数、条数或格式要求时，先理解限制针对整个答复还是其中一部分。'
    '有总字数范围时，给内容安排范围中间的目标并留余量；分条内容要把总目标除以条数，'
    '给每条分配合适的篇幅，各条相加须满足总范围，不要把总字数误当成每一条的字数。'
    '保留必要信息，删去重复和空泛修饰；输出前尽量按用户指定的口径核对并精简或补充，'
    '避免额外标题、注释或解释导致超出限制。没有实际计数工具时不要声称已经精确计数。'
)

_TOTAL_HANZI = re.compile(
    r'(?:总计|合计|总共|共计)\s*(\d{1,5})\s*(?:至|到|[-~～—–－])\s*(\d{1,5})\s*(?:个)?汉字'
)
_ITEMS = re.compile(r'(?<![每第\d一二两三四五六七八九十])'
                    r'(\d{1,2}|[一二两三四五六七八九十]{1,2})\s*(?:条|行|项)')
_CHINESE_NUMBERS = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6,
                    '七': 7, '八': 8, '九': 9, '十': 10, '十一': 11, '十二': 12}


def explicit_hanzi_list_bounds(text):
    """Infer only an unambiguous total-Hanzi range and matching item count.

    The result is a planning suggestion, not a claim that the model counted or
    satisfied the output. Ambiguous, quoted or per-item requirements fall back
    to the unchanged general guidance.
    """
    if (not isinstance(text, str) or not text or len(text) > 2000 or
            any(mark in text for mark in ('```', '“', '”', '「', '」', '《', '》', '"')) or
            re.search(r'(?:每|各)\s*(?:一)?(?:条|行|项)', text)):
        return None
    ranges = list(_TOTAL_HANZI.finditer(text))
    if len(ranges) != 1:
        return None
    counts = set()
    for match in _ITEMS.finditer(text):
        raw = match.group(1)
        value = int(raw) if raw.isascii() and raw.isdigit() else _CHINESE_NUMBERS.get(raw)
        if value is None:
            return None
        counts.add(value)
    if len(counts) != 1:
        return None
    count = counts.pop()
    low, high = map(int, ranges[0].groups())
    if not (2 <= count <= 12 and count * 10 <= low <= high <= 10000):
        return None
    return count, low, high


def text_guidance_for(text):
    """Add a conservative arithmetic suggestion when the request is explicit."""
    bounds = explicit_hanzi_list_bounds(text)
    if bounds is None:
        return TEXT_CONSTRAINT_GUIDANCE
    count, low, high = bounds
    total = (low + high) // 2
    per_item = total // count
    return (TEXT_CONSTRAINT_GUIDANCE + '\n'
            f'若用户的限制适用于整份答复，可先以总计约{total}个汉字为目标，'
            f'分成{count}项时每项平均约{per_item}个汉字；以用户原始要求为准，'
            '这是篇幅规划，不是实际计数或通过检查的声明。')


def text_guidance_for_context(context):
    """Use the latest user message; earlier source material is not a new instruction."""
    if isinstance(context, list):
        for item in reversed(context):
            if isinstance(item, dict) and item.get('role') == 'user':
                return text_guidance_for(item.get('content'))
    return TEXT_CONSTRAINT_GUIDANCE
