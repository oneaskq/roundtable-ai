"""A member's false claim of searching or testing fails the step; ordinary discussion does not."""
from django.test import SimpleTestCase

from .decision_team_engine import DRAFT_SITUATION_FIELDS, reject_unperformed_action_claims
from .provider import ProviderError


class UnperformedActionClaimTests(SimpleTestCase):
    def test_member_claims_of_unavailable_operations_are_rejected(self):
        for text in ('我已搜索最新政策，发现报名截止日期已延后。',
                     '我已经帮你搜索了两家公司的招聘信息。',
                     '我刚才在网上搜索了一下，A 公司评价不错。',
                     '我实际运行了代码，结果是 42。',
                     '我已对此进行检索，没有发现相关报道。',
                     '我已经测试过两种方案，A 更快。',
                     '我已通过了功能测试。',
                     '已通过功能测试。',
                     '我已上网查询过，结果如下。',
                     '我已部署到测试环境。',
                     '本人已亲自测试，效果稳定。',
                     '已联网搜索，最新政策为每周三天。',
                     '刚刚上网查询过，B 的地址在市中心。',
                     '已运行代码验证，结果正确。',
                     '经实测，A 方案每月节省 200 元。',
                     '{"recommendation":"我已检索相关资料，建议选 A","claims":[]}'):
            with self.subTest(text=text), self.assertRaises(ProviderError):
                reject_unperformed_action_claims(text)

    def test_colloquial_claims_of_looking_something_up_are_rejected(self):
        for text in ('我查了一下，官网写的是每周三天。',
                     '我搜了一下，A 公司评价不错。',
                     '我在网上查到 A 公司去年裁员。',
                     '我刚查过最新政策，报名已延后。',
                     '我已经查阅了最新的招聘信息。',
                     '我看了官网，最新政策是每周三天。',
                     '我已核实官网信息，截止日期是 12 月 1 日。',
                     '根据我的搜索结果，A 公司去年裁员。',
                     '根据我的测试结果，A 更稳定。',
                     '我这边跑了一下代码，结果是 42。',
                     '我这边测了一下，A 更快。',
                     '我亲自试过，A 更快。',
                     '我看了资料，已联网搜索，结果如下。',
                     '另外，已联网搜索，结果如下。',
                     '{"recommendation":"我已联网搜索 W1，结果如下"}',
                     '```json\n{"reply":"我查到 A 公司去年裁员。"}\n```'):
            with self.subTest(text=text), self.assertRaises(ProviderError):
                reject_unperformed_action_claims(text)

    def test_reading_the_given_material_is_not_a_search(self):
        for text in ('我检索了你提供的 S1，里面写明每周到岗三天。',
                     '我已检索 W1，其中没有提到截止日期。',
                     '我查了一下 P1C1 引用的原文，S1 没有这句话。',
                     '我查了资料里的数字，合计 600 元。',
                     '我搜集了你给的三条信息。'):
            with self.subTest(text=text):
                reject_unperformed_action_claims(text)

    def test_clauses_sharing_another_subject_and_descriptions_are_allowed(self):
        for text in ('先别动核心模块。你的 App 已上线半年，已通过外部测试，改动风险高。',
                     '已通过功能测试的版本不能再改动。',
                     '已运行代码的话，先贴出完整报错。',
                     '我测算了一下，每月多花约 600 元。',
                     '我检查了一下两份方案的前提。',
                     '我推测过两种情况。',
                     '我没有查过官网，以下只是推理。',
                     '如果我已经测试过就好了，但本轮没有工具。'):
            with self.subTest(text=text):
                reject_unperformed_action_claims(text)

    def test_the_users_situation_in_a_draft_is_not_the_member_speaking(self):
        draft = ('{"mode":"discussion","goal":"我已上线一个小程序，想判断是否迁移到云服务",'
                 '"assumptions":"已运行代码，但报错信息未提供","questions":["报错信息能贴一下吗？"]}')
        reject_unperformed_action_claims(draft, skip=DRAFT_SITUATION_FIELDS)
        with self.assertRaises(ProviderError):
            reject_unperformed_action_claims(draft)
        # The draft's own question to the user is still the member speaking.
        with self.assertRaises(ProviderError):
            reject_unperformed_action_claims('{"goal":"选择方案","questions":["我已联网搜索过，你看对吗？"]}',
                                             skip=DRAFT_SITUATION_FIELDS)

    def test_facts_about_the_user_and_system_retrieval_are_allowed(self):
        for text in ('你提到系统已经部署到生产环境，那么回滚成本较高。',
                     'B 公司的产品已上线两年，口碑稳定。',
                     '如果你已经搜索过招聘信息，可以对比一下。',
                     '根据系统已检索到的资料 W1，政策已更新。',
                     '系统已经联网检索到 W1。',
                     '我已阅读系统检索到的 W1 资料。',
                     '我国已上线全国统一的报名平台。',
                     '我们已经上线的系统需要先评估迁移成本。',
                     '你告诉我已经部署好了，那就先做回滚预案。',
                     '我已经看过你提供的部署方案。',
                     '该药物已通过临床测试。',
                     '代码已测试通过，可以合并。',
                     '该团队已运行代码，日志见附件。',
                     '该团队已通过功能测试，日志见附件。',
                     '用户已运行代码，报错信息见资料。'):
            with self.subTest(text=text):
                reject_unperformed_action_claims(text)

    def test_negated_statements_are_allowed(self):
        for text in ('我没有联网，无法确认。', '我无法搜索或运行代码。',
                     '我没有实际测试，以下是推理。', '本轮未经实测，结论仅供参考。'):
            with self.subTest(text=text):
                reject_unperformed_action_claims(text)

    def test_quoted_first_person_material_is_not_treated_as_a_member_claim(self):
        for text in ('{"evidence":[{"source":"S1","quote":"我已部署过三个项目","scope":"简历"}]}',
                     '你在简历里写了“我已经测试过三套系统”，这一点面试时可能被追问。',
                     # JSON escapes ASCII quotes; they are quotes again once decoded.
                     '{"recommendation":"你说\\"我已经测试过两种方案\\"，那 A 更快可以作为依据。"}',
                     # An assignment focus is verbatim text from the user's question.
                     '{"assignments":[{"member":"p1","angle":"feasibility","focus":"我已上线一个小程序"}]}'):
            with self.subTest(text=text):
                reject_unperformed_action_claims(text)

    def test_human_review_claims_are_still_rejected(self):
        with self.assertRaises(Exception):
            reject_unperformed_action_claims('本轮结论已经过人工复核。')
