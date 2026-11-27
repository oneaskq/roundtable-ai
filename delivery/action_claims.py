"""Find a member's own claim of a tool it does not have.

Members can't browse, run code, test or deploy. Their spoken text must not
say they did. These are not the member's claims: facts about the user's
situation ("你的 App 已上线半年"), retrieval the system really performed
("系统已检索到 W1"), reading the material it was given ("我检索了你提供的 S1")
and quoted words ("你说“我已经测试过”").

Pure text logic without Django imports, so it can be tested on its own.
"""
import json
import re


# The speaker: 我/本人/笔者, not 我们/我国/我司 and not 你我/告诉我/自我.
SELF = r'(?<![自你您他她诉])(?:我(?![们国省市县区校司院方军厂行部局所])|本人|笔者)'
_MARKER = r'(?:刚(?:刚|才)?|也|还|这边)?(?:已经?|刚(?:刚|才)?|实际(?:上)?|亲自|特意|专门)'
_LINK = (r'(?:已经?|经|完成|通过|进行了?|实际|联网|上网|在线|在网上|网上|外部|'
         r'帮你|为你|替你|给你|帮您|为您|对此|对其|亲自|自行|去|了){0,3}')
# Colloquial forms need no 已 ("我查了一下", "我这边跑了代码"), but do need an aspect marker.
_SAID = (r'(?:这边|刚(?:刚|才)?|也|还|又|先|已经?|亲自|实际(?:上)?|特意|专门|自己|'
         r'在网上|网上|上网|在线|去|帮你|为你|替你|给你|帮您|为您|对此){0,3}')
_ASPECT = r'(?:了|过|到)'
_LOOKUP = (rf'{SELF}{_MARKER}{_LINK}(?:搜索|检索|浏览(?:了)?网页)'
           rf'|{SELF}{_SAID}(?:搜(?:索)?{_ASPECT}|检索{_ASPECT}|查(?:询|阅|证|看)?{_ASPECT}'
           rf'|上网查|网上查|浏览{_ASPECT}网页)')
_RAN = (r'(?:运行|执行|跑)(?:了|过)(?:一下|下)?(?:(?:这|那)(?:段|个|份|项|组))?'
        r'(?:代码|程序|脚本|测试|用例|单测)')
_ACTION = (rf'{SELF}{_MARKER}{_LINK}(?:运行(?:了)?(?:代码|程序|脚本)|执行(?:了)?(?:程序|代码|脚本|测试)|'
           r'实测|(?:外部|实际|功能|程序)?测试|部署|上线)'
           rf'|{SELF}{_SAID}(?:{_RAN}|测(?:了|过)'
           r'|(?:亲自|实际(?:上)?|实地|自己)试(?:了|过|用)'
           r'|(?:看|浏览|访问|打开|登录)(?:了|过)(?:一下|下)?(?:官网|官方网站|网站|网页|官方公告|新闻|报道)'
           r'|(?:核实|核查|查证|验证)(?:了|过)?(?:一下|下)?(?:官网|官方|网站|网页|网上|线上|最新|实时))'
           rf'|(?:根据|据|按照?|从){SELF}(?:的|这边的|刚才的)?(?:搜索|检索|联网查询|查询|实测)'
           rf'|{SELF}(?:的|这边的|刚才的)(?:搜索|检索|联网查询|查询|实测|测试)结果')
# A clause without a subject that can only describe the speaker's own tools.
_TOOL_CLAIM = (r'(?:(?:已经?|经过?|刚(?:刚|才)?)'
               r'(?:(?:联网|上网|在网上|网上)(?:搜索|检索|查询|查证)|(?:运行|执行)(?:了)?(?:代码|程序|脚本)'
               r'|(?:通过|完成)了?(?:外部|实际|功能|程序)?测试)'
               r'|经(?:过)?(?:实测|实际测试))')
# "已通过功能测试的版本" describes a thing, not an action the speaker took.
_BARE = rf'(?:{SELF}|(?<!\w))(?:本轮|本次)?{_TOOL_CLAIM}(?!的)'
_CLAIM = re.compile(rf'(?P<lookup>{_LOOKUP})|(?P<action>{_ACTION})|(?P<bare>{_BARE})')

_SELF_AT = re.compile(SELF)
_NETWORK = re.compile(r'联网|上网|网上|在线|网页|外部')
# Reading what the member was given, right after the verb: "我已检索 W1", "我查了你提供的资料".
_MATERIAL_COUNT = r'[\d一二两三四五六七八九十百几]*[份个篇张本]'
# A possessive alone does not establish that the member received the material ("你的公司官网").
_MATERIAL_REFERENCE = (
    rf'(?:(?:你|您|用户)的(?:这|那)?|这|那|上述|现有|已有|所给|本轮)(?:些|{_MATERIAL_COUNT})?'
    rf'(?:附件|资料|材料|原文|文件|文档|表格)|{_MATERIAL_COUNT}附件')
_PROVIDED = re.compile(
    r'\s*(?:了|过|到)?(?:一下|下)?\s*(?:'
    r'(?:你|您|用户)(?:刚刚?|刚才|之前)?(?:提供|给我?|发|贴|上传|分享|附|列|写|引用)(?:给我|出|来|上)?了?的|'
    r'系统(?:提供|检索到|读取|找到)了?的|给定的?|提供的|附件|'
    rf'{_MATERIAL_REFERENCE}|'
    r'(?:资料|材料|原文|上文|前文|对话|聊天记录)(?:里|中)|'
    r'[SWUBEPR]\d)')
_CONDITIONAL = re.compile(r'(?:如果|假如|假设|要是|若是|倘若|若|等到?)$')
_SENTENCE_END = re.compile(r'[。！？!?；;\n]')
_LEADING = re.compile(r'^(?:[\s，,、：:“”]|另外|此外|同时|而且|并且|然后|随后|首先|其次|最后|'
                      r'目前|现在|刚才|刚刚|这边|对此|为此|所以|因此|好的|嗯|是的)+')
# Quoted or verbatim words are someone else's ("简历里写着“我已部署过三个项目”").
_QUOTED = re.compile(r'“[^”\n]{0,300}”|「[^」\n]{0,300}」|『[^』\n]{0,300}』|"[^"\n]{0,300}"')

# Verbatim source excerpts, verbatim user text and search keywords are not speech.
NOT_SPOKEN = frozenset({'quote', 'focus', 'public_search_query'})


def _is_claim(text, match):
    start = match.start()
    if _CONDITIONAL.search(text[max(0, start - 3):start]) or text.startswith('的话', match.end()):
        return False  # "如果我已经测试过就好了"
    if match.group('lookup'):
        return bool(_NETWORK.search(match.group(0))) or not _PROVIDED.match(text, match.end())
    if match.group('bare') and not _SELF_AT.match(text, start):
        # Without its own subject, the clause shares the sentence's subject:
        # "你的 App 已上线半年，已通过外部测试" is about the App.
        boundary = max((m.end() for m in _SENTENCE_END.finditer(text, 0, start)), default=0)
        subject = _LEADING.sub('', text[boundary:start])
        return not subject or bool(_SELF_AT.match(subject))
    return True


def unperformed_claim(text):
    """Return the first wording that claims an unavailable operation, or None."""
    text = _QUOTED.sub('“”', text)
    for match in _CLAIM.finditer(text):
        if _is_claim(text, match):
            return match.group(0)
    return None


def spoken_texts(value, skip=()):
    """Every string a member says in a parsed output, without verbatim fields."""
    skip = NOT_SPOKEN | frozenset(skip)
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            stack.extend(entry for key, entry in item.items() if key not in skip)
        elif isinstance(item, list):
            stack.extend(item)


def unperformed_claim_in(value, skip=()):
    """Check a parsed JSON output; ``skip`` names fields describing the user's situation."""
    for text in spoken_texts(value, skip):
        if found := unperformed_claim(text):
            return found
    return None


def unperformed_claim_in_output(output, skip=()):
    """Check raw output: a JSON object or array field by field, otherwise as prose."""
    raw = output.strip()
    if raw.startswith('```') and raw.endswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    if raw.startswith(('{', '[')):
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            value = None
        if isinstance(value, (dict, list)):
            return unperformed_claim_in(value, skip)
    return unperformed_claim(output)
