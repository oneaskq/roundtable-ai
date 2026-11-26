"""Recognize explicit Office/PDF output requests before reserving model calls."""
import re
import unicodedata


# Only the deliverable changes. An input named in the same request stays intact.
FORMATS = (
    ('PDF', 'HTML', r'pdf'),
    ('XLSX', 'CSV', r'xlsx|excel|电子表格|工作簿'),
    ('XLS', 'CSV', r'xls'),
    ('PPTX', 'Markdown', r'pptx|powerpoint|演示文稿|幻灯片'),
    ('PPT', 'Markdown', r'ppt'),
    ('DOCX', 'Markdown', r'docx|word'),
    ('DOC', 'Markdown', r'doc'),
)
_ALTERNATIVES = {name: alternative for name, alternative, _ in FORMATS}
_TARGET = re.compile(r'(?<![a-z0-9])(?:' + '|'.join(f'(?P<{name}>{pattern})'
                     for name, _, pattern in FORMATS) + r')(?![a-z0-9])', re.I)
_ACTION = re.compile(r'生成|制作|创建|导出|输出|交付|转为|转换为|转成|改为|改成|保存为|写成|整理成|'
                     r'做(?:成)?|写一|给我|发我|需要|想要|'
                     r'\b(?:create|make|build|generate|export|deliver|provide|convert|turn|save|write|give me|send me)\b', re.I)
_AFTER_TARGET = re.compile(r'^\s*(?:文件|文档|表格|格式|附件|形式)?\s*(?:导出(?!为|成)|交付|输出|提供|发给我|给我下载)')
_INPUT = re.compile(r'分析|核对|读取|查看|阅读|总结|提取|解析|支持|导入|上传|解读|根据|基于|参考|检查|'
                    r'\b(?:from|input|uploaded|attached|based on|about|read|parse|import|'
                    r'(?:summary|highlights|contents|analysis) of)\b', re.I)
_ABOUT = re.compile(r'如何|怎么|为什么|怎样|\b(?:how (?:to|do|can)|why)\b', re.I)
_NEGATIVE = re.compile(r'(?:不要|不用|无需|不必|不需要|别|do not|don[’\']t)\s*$', re.I)
_SUBJECT = re.compile(r'^\s*(?:(?:文件|文档|表格|格式|附件)?\s*的\s*(?:摘要|内容|分析|教程|用法|说明|脚本|代码)|'
                      r'(?:文件|文档)?[里中内](?:的|面)|解析器|转换器|生成器|阅读器|公式|函数|'
                      r'(?:converter|generator|reader|parser|count|cloud|formula|function)\b)', re.I)
_INPUT_CONVERSION = re.compile(r'^\s*(?:(?:file|document|workbook|slides?)\s+)?(?:to|into|as)\s+|'
                              r'^\s*(?:文件|材料|文档|表格)?\s*(?:转(?:换)?[成为]|导出[成为]|生成|制作)', re.I)
_CLAUSE = re.compile(r'[^，,。！？!?；;\n]+')
_QUOTE = re.compile(r'^[ \t]*`{3}[^\n]*\n[\s\S]*?(?:^[ \t]*`{3}[^\n]*|\Z)|^[ \t]*>[^\n]*', re.M)
_TEXT_ONLY = re.compile(r'(?:只要|只需|直接用|仅需)(?:文字|文本)|(?:不要|不需要|不用)(?:生成|交付)?(?:文件|附件)|\btext only\b', re.I)


def delivery_format_offer(content):
    # Only inspect the request outside code and quoted examples.
    visible = _QUOTE.sub(lambda m: ' ' * len(m[0]), content)
    visible = ''.join(unicodedata.normalize('NFKC', ch) if '\uff01' <= ch <= '\uff5e' else ch for ch in visible)
    requested = {}
    for clause in _CLAUSE.finditer(visible):
        part = clause[0]
        if _TEXT_ONLY.search(part):
            requested.clear()
            continue
        actions = list(_ACTION.finditer(part))
        for target in _TARGET.finditer(part):
            before, after = part[:target.start()], part[target.end():]
            action = next((item for item in reversed(actions) if item.end() <= target.start()), None)
            if (_SUBJECT.search(after) or _ABOUT.search(before) or _INPUT_CONVERSION.search(after)
                    or target[0].lower() == 'word' and re.search(r'\d[ -]$', before)):
                continue
            if action:
                object_text = part[action.end():target.start()]
                if action[0] in ('给我', '需要', '想要') and re.match(r'\s*(?:简单|详细)?(?:解释|说明|介绍|对比|比较)', object_text):
                    continue
                if action[0].lower() in ('convert', 'turn', 'save', 'export'):
                    object_text = re.split(r'\b(?:to|as|into)\s+', object_text, flags=re.I)[-1]
                if _NEGATIVE.search(part[:action.start()]) or _INPUT.search(object_text):
                    continue
            elif not _AFTER_TARGET.search(after) or _INPUT.search(before) or _NEGATIVE.search(before):
                continue
            requested[target.lastgroup] = _ALTERNATIVES[target.lastgroup]
    if not requested:
        return {'requested_formats': []}
    changes = '，'.join(f'{name} 改用 {alternative}' for name, alternative in requested.items())
    goal = f'{content}\n\n本轮交付格式已调整为：{changes}。其余内容要求沿用原需求。'
    return {'requested_formats': list(requested), 'replacement_formats': list(dict.fromkeys(requested.values())),
            'format_description': changes, 'replacement_goal': goal}
