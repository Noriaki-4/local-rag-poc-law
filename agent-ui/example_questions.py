"""UIに表示する法令横断の質問例と、その難易度分類。

難易度は「回答にたどり着くまでに横断する資料の構造」で分ける。文章量や論点の数ではなく、
Lv.1=2つの法律の読み分け、Lv.2=法律と下位法令、Lv.3=法律・政令・府令の3階層、
Lv.4=法令に加えて行政のガイドライン（PDF資料）まで到達が必要、という基準。

質問文には原則として参照先の法令名・資料名を書かない。法令名を書くと検索対象を人が指定したことになり、
「必要な資料を自力で見つけられるか」を試せなくなるため。ただし、既知の改正元から影響先を探す設問では、
その改正元を検索起点として質問に含める。expected、required_evidence、answer_points は答え合わせ専用で、
質問文やAgent APIへのリクエストには含めない
（tests/test_example_questions.py で担保）。

採点項目を全て満たしたかを1回ごとに判定するが、検索・LLMには揺らぎがあるため、
システムの能力は1回の成否ではなく複数回の到達率で判断する。
少人数私募は定義府令への到達が3回中2回だった（2026-07-25 実測）。
"""

import unicodedata
from dataclasses import dataclass
from typing import Any, NamedTuple


class QuestionLevel(NamedTuple):
    level: int
    name: str
    criteria: str


@dataclass(frozen=True)
class EvidenceRequirement:
    """回答根拠として必要な条文・資料。

    content_unit_prefixes は同じ条の項・号を許容する。prefix の直後がハイフンの場合だけ
    一致させるため、第2条が第27条に誤一致することはない。
    """

    name: str
    content_unit_prefixes: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnswerPoint:
    """回答本文に必要な要点。alternatives の各要素はAND条件、その外側はOR条件。"""

    name: str
    alternatives: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class ExampleQuestion:
    level: int
    title: str
    question: str
    expected: str
    required_evidence: tuple[EvidenceRequirement, ...]
    answer_points: tuple[AnswerPoint, ...]
    legal_as_of: str = "2026-07-26"
    known_source_is_search_origin: bool = False


def _articles(name: str, *content_unit_prefixes: str) -> EvidenceRequirement:
    return EvidenceRequirement(name=name, content_unit_prefixes=content_unit_prefixes)


def _document(name: str, *document_ids: str) -> EvidenceRequirement:
    return EvidenceRequirement(name=name, document_ids=document_ids)


def _point(name: str, *alternatives: tuple[str, ...]) -> AnswerPoint:
    return AnswerPoint(name=name, alternatives=alternatives)


LEVELS: tuple[QuestionLevel, ...] = (
    QuestionLevel(
        level=1,
        name="一般法と特別法の読み分け",
        criteria="2つの法律のどちらが適用されるか、どう補い合うかを読み分ける。",
    ),
    QuestionLevel(
        level=2,
        name="法律と下位法令の突き合わせ",
        criteria="法律が定める要件と、施行規則・内閣府令が定める具体的な手続を突き合わせる。",
    ),
    QuestionLevel(
        level=3,
        name="法律・政令・府令の3階層",
        criteria="委任の連鎖を法律から政令・府令までたどり、要件・例外・手続を組み立てる。",
    ),
    QuestionLevel(
        level=4,
        name="法令と行政ガイドラインの突き合わせ",
        criteria="条文だけでは判断基準が読み取れず、監督指針・Q&A・基準などのPDF資料まで参照する。",
    ),
)

EXAMPLE_QUESTIONS: tuple[ExampleQuestion, ...] = (
    ExampleQuestion(
        level=1,
        title="土地を借りる期間の違い",
        question=(
            "建物を建てるために土地を借りる場合と、資材置き場のように建物を建てない目的で"
            "土地を借りる場合とで、契約できる期間のルールはどう違いますか。"
            "根拠となる条文も示してください。"
        ),
        expected="民法＋借地借家法",
        required_evidence=(
            _articles("普通借地権の存続期間（借地借家法3条）", "law-403AC0000000090-article-3"),
            _articles("賃貸借の最長期間（民法604条）", "law-129AC0000000089-article-604"),
        ),
        answer_points=(
            _point("普通借地権は原則30年", ("30年",), ("三十年",)),
            _point("建物所有目的でない賃貸借は最長50年", ("50年",), ("五十年",)),
            _point("建物所有目的の有無で適用法が分かれる", ("建物", "目的"), ("建物所有",)),
        ),
    ),
    ExampleQuestion(
        level=1,
        title="賃貸住宅を退去するとき",
        question=(
            "アパートの契約が終わるので出ていってほしいと大家から言われました。"
            "大家の側にはどのような条件が必要ですか。退去時の原状回復や敷金の返還が"
            "どう扱われるかも含めて、根拠となる条文とともに教えてください。"
        ),
        expected="借地借家法＋民法",
        required_evidence=(
            _articles("更新拒絶・解約申入れの正当事由（借地借家法28条）", "law-403AC0000000090-article-28"),
            _articles("通常損耗を除く原状回復義務（民法621条）", "law-129AC0000000089-article-621"),
            _articles("敷金返還義務（民法622条の2）", "law-129AC0000000089-article-622_2"),
        ),
        answer_points=(
            _point("大家側の正当事由", ("正当事由",)),
            _point(
                "通常損耗・経年変化は原則として借主負担外",
                ("通常損耗",),
                ("通常の使用", "損耗"),
                ("経年変化",),
                ("経年劣化",),
            ),
            _point("敷金は債務控除後の残額を返還", ("敷金", "返還"), ("敷金", "控除")),
        ),
    ),
    ExampleQuestion(
        level=1,
        title="借地上の建物を売るとき",
        question=(
            "借りている土地の上に建てた自分の家を第三者に売りたいのですが、地主の承諾は"
            "必要ですか。承諾してもらえないときにどうすればよいかも含めて、"
            "根拠となる条文とともに教えてください。"
        ),
        expected="民法＋借地借家法",
        required_evidence=(
            _articles("賃借権譲渡の承諾（民法612条）", "law-129AC0000000089-article-612"),
            _articles("裁判所の代諾許可（借地借家法19条）", "law-403AC0000000090-article-19"),
        ),
        answer_points=(
            _point("地主の承諾が原則必要", ("承諾", "必要"), ("承諾を得",)),
            _point("承諾に代わる裁判所の許可", ("裁判所", "許可"), ("代わる許可",)),
        ),
    ),
    ExampleQuestion(
        level=2,
        title="医薬品会社の法令遵守体制",
        question=(
            "医薬品を製造販売する会社は、法令を守るためにどのような責任者を置き、"
            "どのような社内体制と業務手順を整える必要がありますか。誰が何を担当するのかが"
            "分かるように、根拠となる条文とともに説明してください。"
        ),
        expected="薬機法＋薬機法施行規則",
        required_evidence=(
            _articles("総括製造販売責任者（薬機法17条）", "law-335AC0000000145-article-17"),
            _articles("法令遵守体制（薬機法18条の2）", "law-335AC0000000145-article-18_2"),
            _articles(
                "責任者の連携または法令遵守体制の具体化（施行規則）",
                "law-336M50000100001-article-87",
                "law-336M50000100001-article-98_9",
            ),
        ),
        answer_points=(
            _point("総括製造販売責任者の設置", ("総括製造販売責任者",)),
            _point("品質管理と安全管理の担当・連携", ("品質", "安全", "連携"), ("品質管理", "安全管理")),
            _point("権限明確化と監督体制", ("権限", "監督"), ("法令遵守体制",)),
        ),
    ),
    ExampleQuestion(
        level=2,
        title="医薬品の製造販売業の許可",
        question=(
            "医薬品の製造販売を事業として始めるには、どのような許可が必要ですか。"
            "許可を受けるための基準と、許可申請書に添付する書類や届出の様式も含めて、"
            "根拠となる条文とともに説明してください。"
        ),
        expected="薬機法＋薬機法施行規則",
        required_evidence=(
            _articles("製造販売業の許可（薬機法12条）", "law-335AC0000000145-article-12"),
            _articles("許可の基準（薬機法12条の2）", "law-335AC0000000145-article-12_2"),
            _articles("申請様式・添付書類（施行規則19条）", "law-336M50000100001-article-19"),
        ),
        answer_points=(
            _point("第一種・第二種の許可区分", ("第一種", "第二種"), ("許可", "区分")),
            _point("品質管理・製造販売後安全管理の基準", ("品質管理", "安全管理"), ("品質管理", "製造販売後")),
            _point("様式第九による申請", ("様式第9",), ("様式第九",)),
            _point("具体的な添付書類", ("添付書類",), ("書類を添付",), ("登記事項証明書",)),
        ),
    ),
    ExampleQuestion(
        level=2,
        title="有価証券報告書の提出",
        question=(
            "有価証券報告書は、どのような会社が、いつまでに提出する必要がありますか。"
            "記載しなければならない事項と添付書類も含めて、根拠となる条文とともに"
            "説明してください。"
        ),
        expected="金融商品取引法＋開示府令",
        required_evidence=(
            _articles("有価証券報告書の提出義務（金融商品取引法24条）", "law-323AC0000000025-article-24"),
            _articles("有価証券報告書の様式（開示府令15条）", "law-348M50000040005-article-15"),
            _articles("添付書類（開示府令17条）", "law-348M50000040005-article-17"),
        ),
        answer_points=(
            _point("提出対象となる会社", ("上場",), ("募集", "届出"), ("提出義務者",)),
            _point("原則として事業年度経過後3か月以内", ("3か月以内",), ("三か月以内",)),
            _point("記載事項と添付書類", ("記載事項", "添付書類"), ("様式", "添付")),
        ),
    ),
    ExampleQuestion(
        level=3,
        title="少人数への株式の勧誘（少人数私募）",
        question=(
            "新しく発行する自社株式を、50名に満たない少数の相手にだけ引き受けてもらう場合、"
            "有価証券届出書の提出は不要になりますか。勧誘の相手方の人数の数え方と、"
            "取得者に課される転売制限の内容や勧誘時に必要な告知も含めて、"
            "根拠となる条文とともに説明してください。"
        ),
        expected="金融商品取引法＋施行令＋定義府令",
        required_evidence=(
            _articles("50名基準（施行令1条の5）", "law-340CO0000000321-article-1_5"),
            _articles("過去の勧誘との合算（施行令1条の6）", "law-340CO0000000321-article-1_6"),
            _articles("転売制限措置（施行令1条の7）", "law-340CO0000000321-article-1_7"),
            _articles("転売制限の具体化（定義府令13条）", "law-405M50000040014-article-13"),
            _articles("告知・書面交付（金融商品取引法23条の13）", "law-323AC0000000025-article-23_13"),
        ),
        answer_points=(
            _point("50名未満の基準", ("50名",), ("五十名",)),
            _point(
                "過去3か月の勧誘人数との合算",
                ("3か月", "合算"),
                ("三か月", "合算"),
                ("3か月", "合計"),
                ("3か月", "通算"),
                ("3月以内", "合計"),
                ("3月以内", "通算"),
            ),
            _point("取得者への転売・譲渡制限", ("転売制限",), ("譲渡制限",)),
            _point("未届出の告知と書面交付", ("告知", "書面"), ("届出", "告知")),
        ),
    ),
    ExampleQuestion(
        level=3,
        title="株券を買い集める場合の公開買付け",
        question=(
            "買付者は当社、株券等の発行者は当社とは別の上場会社（対象会社）です。"
            "当社が、取引所金融商品市場外で、対象会社の株券等を複数の所有者から買い付ける場合、"
            "どのような条件で公開買付けが必要になりますか。対象となる株券等の範囲、"
            "主な適用除外、公開買付けが必要な場合の手続を、根拠条文とともに説明してください。"
        ),
        expected="金融商品取引法＋施行令＋公開買付府令",
        required_evidence=(
            _articles("公開買付規制（金融商品取引法27条の2）", "law-323AC0000000025-article-27_2"),
            _articles("適用除外（施行令7条）", "law-340CO0000000321-article-7"),
            _articles(
                "少数所有者・全所有者同意に関する適用除外の詳細（公開買付府令2条の5）",
                "law-402M50000040038-article-2_5",
            ),
            _articles("公開買付開始公告（公開買付府令10条）", "law-402M50000040038-article-10"),
        ),
        answer_points=(
            _point("5%ルール", ("5%",), ("百分の五",), ("5パーセント",)),
            _point("30%ルール", ("30%",), ("百分の三十",), ("30パーセント",)),
            _point("公開買付開始公告と届出書", ("公告", "届出書"), ("公開買付届出書",)),
            _point("主な適用除外", ("適用除外",), ("例外",)),
        ),
    ),
    ExampleQuestion(
        level=3,
        title="役職員への譲渡制限付株式の交付",
        question=(
            "上場会社が自社や子会社の役職員へ、譲渡を一定期間制限した自社株式を報酬として"
            "交付する場合、有価証券の募集・売出しの届出は必要ですか。届出が不要となり得る条件、"
            "対象にできる人の範囲、譲渡制限をいつまで課す必要があるかも含めて、"
            "根拠となる条文とともに説明してください。"
        ),
        expected="金融商品取引法＋施行令＋開示府令",
        required_evidence=(
            _articles("届出免除（金融商品取引法4条1項）", "law-323AC0000000025-article-4-paragraph-1"),
            _articles("役職員向け株式の要件（施行令2条の12）", "law-340CO0000000321-article-2_12"),
            _articles(
                "対象となる子会社の具体化（開示府令2条1項）",
                "law-348M50000040005-article-2-paragraph-1",
            ),
        ),
        answer_points=(
            _point("対象は自社・子会社等の役職員", ("取締役",), ("役職員",), ("使用人",)),
            _point("上場株券等であること", ("上場",)),
            _point("報告書提出までの譲渡制限", ("譲渡制限", "有価証券報告書"), ("譲渡", "半期報告書")),
        ),
    ),
    ExampleQuestion(
        level=3,
        title="少数所有者の場合の公開買付けの適用除外",
        question=(
            "買付者は当社、株券等の発行者は当社とは別の上場会社（対象会社）です。"
            "対象会社の株券等の所有者が少数である場合について、公開買付けが原則として必要となる規定、"
            "適用除外を設ける規定、少数所有者と全所有者同意の具体的条件を定める規定を、"
            "上位から順に根拠条文とともに説明してください。"
        ),
        expected="金融商品取引法＋施行令＋公開買付府令",
        required_evidence=(
            _articles("公開買付規制（金融商品取引法27条の2）", "law-323AC0000000025-article-27_2"),
            _articles("適用除外（施行令7条）", "law-340CO0000000321-article-7"),
            _articles(
                "少数所有者・全所有者同意の具体的条件（公開買付府令2条の5）",
                "law-402M50000040038-article-2_5",
            ),
        ),
        answer_points=(
            _point("法律が公開買付けの原則を定める", ("公開買付け", "原則")),
            _point("施行令が適用除外を定める", ("施行令", "適用除外"), ("政令", "適用除外")),
            _point("所有者が25名未満", ("25名未満",), ("二十五名未満",)),
            _point("全所有者の同意", ("全ての所有者", "同意"), ("全所有者", "同意")),
        ),
        legal_as_of="2026-08-27",
    ),
    ExampleQuestion(
        level=3,
        title="少数所有者の適用除外で使う特別関係者の定義",
        question=(
            "公開買付府令第2条の5第2項では、買付者の所有割合に、買付者以外の者の所有割合を"
            "加えて判定する場面があります。加算対象となる者は、法令上どのような者ですか。"
            "定義元の根拠条文とともに説明してください。"
        ),
        expected="金融商品取引法＋公開買付府令",
        required_evidence=(
            _articles(
                "特別関係者の定義（金融商品取引法27条の2第7項）",
                "law-323AC0000000025-article-27_2",
            ),
            _articles(
                "所有割合合算の規定（公開買付府令2条の5）",
                "law-402M50000040038-article-2_5",
            ),
        ),
        answer_points=(
            _point("買付者と特別関係者の所有割合を合算", ("特別関係者", "合計"), ("特別関係者", "合算")),
            _point(
                "特別関係者の二つの類型",
                ("株式の所有関係", "合意"),
                ("特別の関係", "共同", "合意"),
            ),
        ),
        legal_as_of="2026-08-27",
    ),
    ExampleQuestion(
        level=3,
        title="公開買付開始公告で選択できる公告方法",
        question=(
            "公開買付開始公告について、公開買付者が選択できる公告方法を示してください。"
            "その上で、どの方法を選んでも公告に掲載する必要がある事項と、"
            "選択した方法ごとに守る必要がある条件を区別し、根拠条文とともに説明してください。"
        ),
        expected="金融商品取引法＋施行令＋公開買付府令",
        required_evidence=(
            _articles("公開買付開始公告（金融商品取引法27条の3）", "law-323AC0000000025-article-27_3"),
            _articles("公告方法（施行令9条の3）", "law-340CO0000000321-article-9_3"),
            _articles("公告方法の具体化（公開買付府令9条）", "law-402M50000040038-article-9"),
            _articles("公告の掲載事項（公開買付府令10条）", "law-402M50000040038-article-10"),
        ),
        answer_points=(
            _point("電子公告又は日刊新聞紙を選択できる", ("電子公告", "日刊新聞紙")),
            _point("電子公告の継続期間と新聞による周知", ("電子公告", "末日", "日刊新聞紙")),
            _point(
                "新聞公告に使用する日刊新聞紙の条件",
                ("二以上", "日刊新聞紙"),
                ("二つ以上", "日刊新聞紙"),
                ("全国", "一以上"),
                ("全国", "一つ以上"),
            ),
            _point("公告方法にかかわらない掲載事項", ("掲載事項", "公開買付届出書", "縦覧")),
        ),
        legal_as_of="2026-08-27",
    ),
    ExampleQuestion(
        level=3,
        title="公開買付開始公告の社内方針",
        question=(
            "当社は、公開買付開始公告を原則として電子公告で行い、公告には買付け等の価格と期間だけを"
            "掲載するという社内方針を作ろうとしています。この方針について、会社が選択できる部分と"
            "法令上省略できない部分を区別し、修正が必要な点を根拠条文とともに説明してください。"
        ),
        expected="金融商品取引法＋施行令＋公開買付府令",
        required_evidence=(
            _articles("公開買付開始公告（金融商品取引法27条の3）", "law-323AC0000000025-article-27_3"),
            _articles("公告方法（施行令9条の3）", "law-340CO0000000321-article-9_3"),
            _articles("公告方法の具体化（公開買付府令9条）", "law-402M50000040038-article-9"),
            _articles("公告の掲載事項（公開買付府令10条）", "law-402M50000040038-article-10"),
        ),
        answer_points=(
            _point("電子公告は選択できる", ("電子公告", "選択")),
            _point("電子公告の実施条件", ("電子公告", "継続"), ("電子公告", "日刊新聞紙")),
            _point("価格と期間以外も省略できない", ("価格", "期間", "だけ"), ("価格", "期間", "以外")),
            _point("届出書の写しを縦覧に供する場所", ("届出書", "写し", "縦覧")),
        ),
        legal_as_of="2026-08-27",
    ),
    ExampleQuestion(
        level=3,
        title="公開買付開始公告の改正影響",
        question=(
            "金融商品取引法第二十七条の三の公開買付開始公告について、公告方法又は公告事項に関する規定が"
            "改正されたと仮定します。現在のデータセットの中から、改正内容によって見直しが必要になる可能性が"
            "ある政令・内閣府令の条文を挙げ、それぞれが公告方法と公告事項のどちらを具体化しているか、"
            "根拠とともに説明してください。"
        ),
        expected="金融商品取引法＋施行令＋公開買付府令",
        required_evidence=(
            _articles("改正元（金融商品取引法27条の3）", "law-323AC0000000025-article-27_3"),
            _articles("公告方法（施行令9条の3）", "law-340CO0000000321-article-9_3"),
            _articles("公告方法の具体化（公開買付府令9条）", "law-402M50000040038-article-9"),
            _articles("公告事項の具体化（公開買付府令10条）", "law-402M50000040038-article-10"),
        ),
        answer_points=(
            _point("施行令9条の3は公告方法を具体化する", ("施行令第9条の3", "公告方法"), ("施行令第九条の三", "公告方法")),
            _point("公開買付府令9条は公告方法を具体化する", ("府令第9条", "公告方法"), ("府令第九条", "公告方法")),
            _point("公開買付府令10条は公告事項を具体化する", ("府令第10条", "公告事項"), ("府令第十条", "公告事項")),
            _point("改正要否は改正内容との比較が必要", ("改正", "確認"), ("改正内容", "比較")),
        ),
        legal_as_of="2026-08-27",
        known_source_is_search_origin=True,
    ),
    ExampleQuestion(
        level=3,
        title="非居住公開買付者の社内業務手順",
        question=(
            "非居住者である公開買付者が、公開買付けを開始するための社内業務手順を作ろうとしています。"
            "公告方法の選択、公告への掲載事項、公開買付届出書の提出時期及び国内代理人の選任について、"
            "手順に含めるべき事項を根拠条文とともに整理してください。"
        ),
        expected="金融商品取引法＋施行令＋公開買付府令",
        required_evidence=(
            _articles("公開買付開始公告と届出書（金融商品取引法27条の3）", "law-323AC0000000025-article-27_3"),
            _articles("公告方法（施行令9条の3）", "law-340CO0000000321-article-9_3"),
            _articles("公告方法の具体化（公開買付府令9条）", "law-402M50000040038-article-9"),
            _articles("公告の掲載事項（公開買付府令10条）", "law-402M50000040038-article-10"),
            _articles("非居住者の国内代理人（公開買付府令11条）", "law-402M50000040038-article-11"),
        ),
        answer_points=(
            _point("電子公告又は日刊新聞紙を選択する", ("電子公告", "日刊新聞紙")),
            _point("公告の必須掲載事項を確認する", ("掲載事項",), ("公告", "必要な事項")),
            _point("公告日に公開買付届出書を提出する", ("公告", "日", "公開買付届出書", "提出")),
            _point("非居住者は国内代理人を定める", ("非居住者", "国内", "代理人")),
        ),
        legal_as_of="2026-08-27",
    ),
    ExampleQuestion(
        level=4,
        title="退去時の原状回復はどこまで借主負担か",
        question=(
            "賃貸住宅を退去するとき、家具の設置跡や日焼けによる壁紙の変色など、"
            "普通に住んでいて生じた傷みの修繕費まで借主が負担しなければなりませんか。"
            "負担範囲をどう判断するのか、敷金から差し引けるのかも含めて、根拠も示して"
            "教えてください。"
        ),
        expected="民法＋原状回復ガイドライン",
        required_evidence=(
            _articles("通常損耗を除く原状回復義務（民法621条）", "law-129AC0000000089-article-621"),
            _articles("敷金返還義務（民法622条の2）", "law-129AC0000000089-article-622_2"),
            _document("国土交通省の原状回復ガイドライン", "guidance-mlit-restoration"),
        ),
        answer_points=(
            _point("通常損耗・経年変化は原則として借主負担外", ("通常損耗",), ("経年変化",)),
            _point("故意・過失等は借主負担", ("故意", "過失"), ("善管注意義務",)),
            _point("敷金から控除できる範囲", ("敷金", "控除"), ("敷金", "差し引")),
        ),
    ),
    ExampleQuestion(
        level=4,
        title="医薬品の広告で禁止されること",
        question=(
            "医薬品の広告で禁止される表現には、どのようなものがありますか。"
            "まだ承認されていない医薬品を広告できるかも含めて、判断のよりどころとなる"
            "根拠を示して教えてください。"
        ),
        expected="薬機法＋適正広告基準",
        required_evidence=(
            _articles("虚偽・誇大広告の禁止（薬機法66条）", "law-335AC0000000145-article-66"),
            _articles("未承認医薬品の広告禁止（薬機法68条）", "law-335AC0000000145-article-68"),
            _document("医薬品等適正広告基準", "guidance-mhlw-0000179264"),
        ),
        answer_points=(
            _point("虚偽・誇大な表現の禁止", ("虚偽", "誇大"), ("誇大広告",)),
            _point("承認範囲を超える効能効果の禁止", ("承認", "範囲", "効能"), ("承認", "範囲", "効果")),
            _point("未承認医薬品の広告禁止", ("未承認", "禁止"), ("承認前", "広告")),
        ),
    ),
    ExampleQuestion(
        level=4,
        title="高齢の顧客へのリスク商品の勧誘",
        question=(
            "高齢のお客様に値動きの大きい金融商品を勧誘する場合、法令上どのような配慮が"
            "必要ですか。社内で整えておくべき勧誘の手続や管理体制も含めて、"
            "根拠を示して教えてください。"
        ),
        expected="金融商品取引法＋監督指針",
        required_evidence=(
            _articles("適合性の原則（金融商品取引法40条）", "law-323AC0000000025-article-40"),
            _document("金融商品取引業者等向けの総合的な監督指針", "guidance-fsa-financial-instruments-business"),
        ),
        answer_points=(
            _point("高齢顧客には慎重な勧誘が必要", ("高齢", "慎重"), ("高齢顧客", "配慮")),
            _point("知識・経験・財産等に照らす適合性", ("適合性",), ("知識", "経験", "財産")),
            _point("社内規則とモニタリング体制", ("社内規則", "モニタリング"), ("管理体制", "勧誘")),
        ),
    ),
)


class ExpectedSourceStatus(NamedTuple):
    name: str
    reached: bool


@dataclass(frozen=True)
class ExampleEvaluation:
    """例題回答の採点結果。検索・回答生成が終わった後にだけ作る。"""

    source_statuses: tuple[ExpectedSourceStatus, ...]
    evidence_statuses: tuple[ExpectedSourceStatus, ...]
    answer_point_statuses: tuple[ExpectedSourceStatus, ...]

    @property
    def passed(self) -> bool:
        statuses = (
            *self.source_statuses,
            *self.evidence_statuses,
            *self.answer_point_statuses,
        )
        return bool(statuses) and all(status.reached for status in statuses)


# expected の名前と、投入済み資料の documentId の対応。
# タイトルの部分一致では判定できない(「〜法施行規則」は「〜法」を含み、
# 「公開買付けに関するQ&A」は公開買付府令ではない)ため、IDの完全一致で判定する。
EXPECTED_SOURCE_DOCUMENT_IDS: dict[str, str] = {
    "民法": "law-129AC0000000089",
    "借地借家法": "law-403AC0000000090",
    "薬機法": "law-335AC0000000145",
    "薬機法施行規則": "law-336M50000100001",
    "金融商品取引法": "law-323AC0000000025",
    "施行令": "law-340CO0000000321",
    "開示府令": "law-348M50000040005",
    "公開買付府令": "law-402M50000040038",
    "定義府令": "law-405M50000040014",
    "原状回復ガイドライン": "guidance-mlit-restoration",
    "適正広告基準": "guidance-mhlw-0000179264",
    "監督指針": "guidance-fsa-financial-instruments-business",
}


def find_example(question: str) -> ExampleQuestion | None:
    """入力された質問文が例題そのものなら、その例題を返す。"""
    normalized = (question or "").strip()
    if not normalized:
        return None
    return next((example for example in EXAMPLE_QUESTIONS if example.question == normalized), None)


def expected_source_status(
    expected: str,
    cited_document_ids: list[str],
) -> tuple[ExpectedSourceStatus, ...]:
    """想定参照先ごとに、引用へ到達できたかを documentId の完全一致で返す。

    引用の件数や法令の種類数は関連性を示さないため、例題では「想定した資料に届いたか」で見る。
    対応するIDが未登録の名前は、誤って到達扱いにせず未到達として扱う。
    """
    cited = {str(document_id) for document_id in cited_document_ids if document_id}
    statuses = []
    for token in expected.split("＋"):
        name = token.strip()
        if not name:
            continue
        document_id = EXPECTED_SOURCE_DOCUMENT_IDS.get(name)
        statuses.append(ExpectedSourceStatus(name=name, reached=document_id in cited))
    return tuple(statuses)


def evaluate_example(
    example: ExampleQuestion,
    citations: list[dict[str, Any]],
    answer_text: str | None,
) -> ExampleEvaluation:
    """文書、必要条文、回答要点の3層で例題を採点する。

    採点情報はこの関数の引数からAgent APIへ送られず、検索結果・回答生成にも影響しない。
    """
    cited_document_ids = [
        str(citation.get("documentId") or "") for citation in citations if citation.get("documentId")
    ]
    cited_content_unit_ids = [
        str(citation.get("contentUnitId") or "")
        for citation in citations
        if citation.get("contentUnitId")
    ]
    evidence_statuses = tuple(
        ExpectedSourceStatus(
            name=requirement.name,
            reached=_evidence_requirement_reached(
                requirement,
                cited_document_ids,
                cited_content_unit_ids,
            ),
        )
        for requirement in example.required_evidence
    )
    normalized_answer = _normalize_answer(answer_text)
    answer_point_statuses = tuple(
        ExpectedSourceStatus(
            name=point.name,
            reached=any(
                all(_normalize_answer(token) in normalized_answer for token in alternative)
                for alternative in point.alternatives
            ),
        )
        for point in example.answer_points
    )
    return ExampleEvaluation(
        source_statuses=expected_source_status(example.expected, cited_document_ids),
        evidence_statuses=evidence_statuses,
        answer_point_statuses=answer_point_statuses,
    )


def _evidence_requirement_reached(
    requirement: EvidenceRequirement,
    cited_document_ids: list[str],
    cited_content_unit_ids: list[str],
) -> bool:
    if any(document_id in requirement.document_ids for document_id in cited_document_ids):
        return True
    return any(
        content_unit_id == prefix or content_unit_id.startswith(f"{prefix}-")
        for content_unit_id in cited_content_unit_ids
        for prefix in requirement.content_unit_prefixes
    )


def _normalize_answer(text: str | None) -> str:
    normalized = "".join(unicodedata.normalize("NFKC", text or "").split()).lower()
    return (
        normalized.replace("ヶ月", "か月")
        .replace("ケ月", "か月")
        .replace("箇月", "か月")
    )


def examples_by_level() -> tuple[tuple[QuestionLevel, tuple[ExampleQuestion, ...]], ...]:
    """レベル定義の順に、そのレベルへ属する質問例をまとめて返す。"""
    return tuple(
        (
            level,
            tuple(example for example in EXAMPLE_QUESTIONS if example.level == level.level),
        )
        for level in LEVELS
    )
