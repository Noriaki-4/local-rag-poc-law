from app.opensearch_client import _explicit_article_ids


def test_explicit_article_ids_associate_article_with_longest_law_title() -> None:
    titles = {
        "law-act": "金融商品取引法",
        "law-order": "金融商品取引法施行令",
        "law-rule": "企業内容等の開示に関する内閣府令",
    }

    assert _explicit_article_ids(
        "金融商品取引法施行令 第二条の十二と"
        "企業内容等の開示に関する内閣府令第十四条の十五を確認する",
        titles,
    ) == (
        "law-order-article-2_12",
        "law-rule-article-14_15",
    )


def test_explicit_article_ids_ignore_article_without_named_law() -> None:
    assert _explicit_article_ids(
        "第二条の十二を確認する",
        {"law-order": "金融商品取引法施行令"},
    ) == ()


def test_explicit_article_ids_resolve_known_law_alias() -> None:
    assert _explicit_article_ids(
        "公開買付府令第2条の5第2項を確認する",
        {
            "law-rule": (
                "発行者以外の者による株券等の公開買付けの開示に関する内閣府令"
            )
        },
    ) == ("law-rule-article-2_5",)
