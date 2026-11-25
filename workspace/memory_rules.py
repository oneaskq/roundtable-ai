"""Small, local rules for user-authored long-term preferences and background."""
import hashlib
import re
import unicodedata


EXPLICIT = re.compile(r'^(?:(?:请|麻烦)?(?:你|圆桌)?(?:帮我)?(?:记住|记一下|记下|记得)(?!吗)|'
                      r'(?:please\s+)?remember\b(?:\s+that\b)?)\s*[，,：:]?\s*', re.I)
STYLE = re.compile(r'回答|回复|答复|沟通|称呼|叫我|先[给说看]结论|开门见山|分点|长话短说|长篇大论|简明扼要|'
                   r'(?:默认|请|偏好|习惯)(?:使?用)?(?:简体中文|繁体中文|中文|英文|英语)|'
                   r'\b(?:responses?|replies|answers?|call me|bullet points?)\b', re.I)
PREFERENCE = re.compile(
    r'^(?:(?:在|对于|关于|选择?|挑|比较|找|做)[^，,。！？]{1,25}[时上]?[,，]?\s*)?'
    r'(?:我(?:的.{0,14}?(?:偏好|习惯|专业|母语|职业|名字|昵称|目标|预算|方向)(?:是|叫)|'
    r'(?:一直|平时|通常|一般|一向|向来|以后|更|最|比较|特别|尤其|不太|不再|不)?'
    r'(?:更?喜欢|更?看重|重视|偏好|偏向|倾向|习惯|优先|在意|擅长|惯用|常用)|'
    r'平时|通常|每(?:天|周|星期|月|个星期)|是一(?:名|个|位)|是(?:大[一二三四]|研[一二三]|学生|老师|教师|程序员|设计师)|'
    r'希望(?:你|圆桌)?(?:以后|今后|一直|每次|默认|回答时)|是[^，,。！？]{1,12}专业的?学生|叫|主修|从事)|'
    r'(?:以后|今后|往后|后续|每次|默认|一直)(?:都)?(?:请|用|叫|回答|回复|希望|先)|'
    r'(?:以后|今后)?请(?:一直|始终|默认|以后)|'
    r"(?:I(?:'m| am) (?:an? |a student\b)|my (?:name|major|goal|native language) is\b|"
    r"I (?:usually|always|prefer|value|prioriti[sz]e|work as|study)\b|(?:always|in future|from now on)\b))", re.I)
PRIVATE = re.compile(r'密码|验证码|身份证|银行卡|住址|家庭地址|手机号|确诊|患有|病史|过敏|'
                     r'password|passcode|api[\s_-]?key|access[\s_-]?token|secret key|diagnosed|'
                     r'[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b(?:sk-[\w-]{8,}|\d{11,19})\b', re.I)
TRANSIENT = re.compile(r'今天|今日|今晚|昨天|明天|这两天|这次|本次|这一次|这回|暂时|刚才|如果|假如|假设|比如|举例|'
                       r'不要记|不用记|别记|忘掉|忘记|发烧|发热|生病|'
                       r'\b(?:today|this time|for now|if|for example|forget)\b', re.I)
QUOTED = re.compile(r'\x60{3}[\s\S]*?(?:\x60{3}|\Z)|\x60[^\x60\n]*\x60|“[^”]*(?:”|$)|「[^」]*(?:」|$)|'
                    r'『[^』]*(?:』|$)|"[^"\n]*"|^[ \t]*>.*$', re.M)
TOPICS = {
    'work': ('实习', '求职', '岗位', '工作机会', '职业', '工程师', '设计师', '程序员', 'offer', '面试', '通勤', 'internship', 'job'),
    'study': ('课程', '课表', '上课', '选课', '学校', '专业', '考试', '复习', '学生', '学习', 'study', 'class'),
    'schedule': ('每周', '时间表', '日程', '排期', '安排', '规划', '计划', 'schedule'),
    'food': ('饮食', '吃饭', '吃什么', '口味', '餐厅', '晚餐', '早餐', '午餐', '菜谱', '食谱', '素食', '甜食', '点餐', 'food', 'meal'),
    'travel': ('旅行', '旅游', '行程', '出游', 'travel', 'trip'),
}
ABOUT = re.compile(r'记得我|我的.{0,3}(?:偏好|习惯|背景|记忆)|你(?:还)?记住了什么|关于我|'
                   r'what do you (?:remember|know) about me|my preferences', re.I)


def topics(text):
    return {key for key, words in TOPICS.items() if any(word in text.lower() for word in words)}


def memory_key(text):
    if STYLE.search(text):
        for key, pattern in [('language', r'中文|英文|英语|汉语|日语|法语|chinese|english|japanese|french'),
                             ('length', r'简短|简洁|精简|详细|长话短说|brief|concise|detailed|short'),
                             ('address', r'称呼|叫我|call me')]:
            if re.search(pattern, text, re.I):
                return 'style-' + key
    if re.search('看重|最在意|优先考虑|prioriti[sz]e', text, re.I):
        subject = sorted(topics(text))
        if len(subject) == 1:
            return 'priority-' + subject[0]
    preference = re.fullmatch(r'我(?:一直|平时|通常|一般|一向|向来)?(?:更|最|比较|特别|不太|不再|不)?喜欢(.+)', text)
    identity = '喜欢' + preference[1] if preference else text
    normalized = re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', identity).casefold())
    return 'fact-' + hashlib.sha256(normalized.encode()).hexdigest()


def candidates(content):
    # Discard quoted material, including an unfinished code fence or block quote.
    clean = re.sub(r'^[@＠](?:deepseek|kimi|qwen|千问|通义千问|圆桌)[,，:\s]+', '', QUOTED.sub(' ', content), flags=re.I)
    clean = re.sub(r'(?im)^[^\n]*(?:示例|原文|翻译|改写|转述|原话|example|translate)[^\n]*[:：][ \t]*\n'
                   r'[\s\S]*?(?=\n\s*\n|\Z)', '', clean)
    seen = set()
    for sentence in re.split(r'(?<=[。！？!?.；;])\s*|\n+', clean):
        line = sentence.strip()
        # A separate request often follows a self-description on the same line.
        line = re.split(r'[,，]\s*(?=请帮|能否|你觉得|你能|帮我|这次|今天|这回)', line, maxsplit=1)[0]
        if not line or line.endswith(('?', '？', '吗', '吗。')) or TRANSIENT.search(line) or PRIVATE.search(line):
            continue
        explicit = EXPLICIT.match(line)
        fact = (line[explicit.end():] if explicit else line).strip(' \t，,：:。.!！；;')
        if not 3 <= len(fact) <= 260 or not (explicit or PREFERENCE.match(fact)):
            continue
        key = memory_key(fact)
        if key not in seen:
            yield key, fact
            seen.add(key)
        if len(seen) >= 3:
            return


def terms(text):
    result = set(re.findall(r'[a-z0-9]{2,}', text.casefold()))
    for word in re.findall(r'[\u3400-\u9fff]+', text):
        result.update(word[index:index + 2] for index in range(len(word) - 1))
    return result - {'喜欢', '习惯', '偏好', '希望', '帮我', '怎么', '一个', '我的', '可以',
                     '请问', '我是', '需要', '什么', 'the', 'and', 'you', 'for', 'with', 'my'}


def relevance(fact, query_terms, query_topics, about):
    overlap = terms(fact) & query_terms
    return (2 * min(5, len(overlap)) + 5 * len(topics(fact) & query_topics)
            + 4 * bool(STYLE.search(fact)) + 3 * about)
