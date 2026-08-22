# 日本語法令検索のAnalyzer・ハイブリッド検索改善案

## 1. 文書の位置づけ

本書は、日本語法令本文をOpenSearchで検索する際のAnalyzerと候補取得方式を見直すための
**実装前レビュー用文書**である。現時点では提案であり、設定値・フィールド構成・合格基準は
確定していない。

対象は、現行方式とLLM主導方式の双方が利用するOpenSearch検索基盤である。LLMの法的判断方法、
回答プロンプト、Graphエッジの意味は本書の対象外とする。

法的な結論を検索スコアだけで確定するものではない。検索は候補取得であり、回答に利用する根拠は
取得した条文本文を確認したうえで選択する。

関連文書:

- [LLM主導の法令調査](llm_directed_legal_retrieval.md)
- [法的論点被覆を考慮した検索](legal_issue_coverage_retrieval.md)
- [法令レイヤー別探索計画](layered_legal_evidence_retrieval_plan.md)

## 2. 問題

### 2.1 現行索引と2つの検索API

索引定義は
`docs/samples/metadata/opensearch_index_mapping.sample.json`にある。
`title`、`heading`、`sectionPath`、`text`はいずれも`text`型だが、Analyzerを明示していない。
そのためOpenSearchの既定Analyzerが使われる。

実機の`_analyze`では、次の検索語がほぼ1文字ずつに分割された。

```text
敷金 賃貸人 返還義務 明渡し後
↓
敷 / 金 / 賃 / 貸 / 人 / 返 / 還 / 義 / 務 / 明 / 渡 / し / 後
```

検索機能全体が不足しているわけではない。現行コードには性質の異なる2つの入口がある。

| API | 利用経路 | 現在の機能 |
|---|---|---|
| `search_by_document_id()` | LLM主導方式のdocumentId指定検索 | BM25のみ、chunk返却 |
| `search_requirement_specs()` | 法令レイヤー別探索 | 直接Article取得、BM25、documentId filter付きvector、RRF、法令Article集約、ガイドchunk返却、`_msearch` batch |

`search_requirement_specs()`には、本書の初版で新規提案としていた機能の多くが既に実装されて
いる。実際の構造問題は、`llm_research_tools.py`のLLM主導経路だけが旧
`search_by_document_id()`を呼び、既存の高機能な入口を再利用していないことである。

`search_by_document_id()`は次のBM25検索だけを使う。

```text
heading^8 / text^2 / sectionPath
```

したがって本計画では第3の検索経路を新設しない。LLM主導経路を
`search_requirement_specs()`系へ寄せ、共通経路へphrase / N-gram laneと新Analyzerを追加する。

なお`search_requirement_specs()`もphrase / N-gram / Cross-Encoderは持っていない。
Cross-Encoderは`agent.py`や`layered_retriever.py`側にあり、LLM主導loopには現在接続されて
いない。

### 2.2 実測した失敗

自然言語例題「賃貸住宅を退去するとき」で、LLMは民法内を次の語で検索した。

```text
敷金 返還 原状回復 賃貸借終了
敷金 賃貸人 返還義務 明渡し後
```

必要な民法622条の2第1項は、どちらの検索でも41候補中25位だった。

一方、検索語を`敷金`だけにすると、同条第2項が1位、第1項が2位になった。条文本文や索引登録が
欠けているのではない。

`multi_match`は`type=best_fields`なので、異なるfieldの得点を単純加算するのではなく、原則として
最良fieldを採る。実際に問題となるのは次である。

- 同一field内で、単文字termのBM25寄与が加算される。
- `minimum_should_match`未指定のため既定ORとなり、一部の一般文字だけでも候補になる。
- `heading^8`が見出しfield内の単文字一致を増幅する。
- BM25のIDFと平均文書長はdocumentId filter後ではなく索引全体の統計を使う。

特に`heading^8`により、「賃貸借」「終了」「義務」等の1文字を多く含む見出しが強く加点された。
候補上限8は失敗を表面化させたが、上限だけを増やすとノイズ、再ランク件数、時間が増えるため、
根本対策にはならない。

Phase 0ではOpenSearchの`_explain`を使い、field別・term別の寄与、IDF、文書長正規化を保存して
この説明を実測で確定する。

### 2.3 LLM主導方式への影響

LLM主導方式では、LLMが対象法令を選び、その法令内を`search_by_document_id()`で検索する。
法令選択が正しくても必要条文が上位に入らないため、LLMへ証拠を提示できない。

2026-07-29に自然言語例題の先頭3問をLLM主導loopだけで実行した結果:

| 問題 | 結果 | 候補段階の必要Article | 最終選択 | 時間 |
|---|---|---:|---:|---:|
| 土地を借りる期間の違い | ready | 2/2 | 2/2 | 20.5秒 |
| 賃貸住宅を退去するとき | LLM timeout | 2/3 | 0/3 | 59.5秒 |
| 借地上の建物を売るとき | ready | 2/2 | 2/2 | 26.6秒 |

3問合計の候補到達は6/7 Articleであり、欠けた1件が民法622条の2だった。readyになった2問は
必要Articleを全て選択できた。

実測では、次の流れで60秒の調査予算を使い切った。

1. LLMが借地借家法28条と民法621条を取得した。
2. 敷金返還の根拠が足りないと判断し、民法内を再検索した。
3. 民法622条の2が再び上位8件から落ちた。
4. 3回目のLLM判断へ進んだ時点で残り19秒となり、LLMがタイムアウトした。

したがって、LLMモデルを変更するだけでは解消しない。必要条文をLLMのコンテキストへ入れる
候補取得の改善が先に必要である。

### 2.4 ガイドの法令内検索を流用している問題

LLM主導方式の`documentIds`指定検索は、現在`search_by_document_id()`を呼ぶ。この関数には
ガイドを0件にする条件が2つある。

1. `docType=law`へ固定している。
2. 通常時に`sectionKey=main`を必須filterとして追加するが、ガイドchunkは`sectionKey`を持たない。

したがってdocTypeだけを直してもガイド検索は復旧しない。LLM主導経路を
`search_requirement_specs()`へ寄せ、`RequirementSearchSpec.doc_type`による既存の法令Article /
ガイドchunk分岐を再利用する。

また、`sectionKey=main`を付けるかどうかは`SUPPLEMENTARY_PROVISION_CUES`とquery文字列だけで
決める手作りヒューリスティックである。附則を静かに落とし得るため、共通経路へ移行する際に
維持・撤去・LLMの明示指定のどれにするかをshadow比較する。ガイドを法的結論の直接根拠として
扱うかどうかは検索APIでは決めない。

## 3. 目標

1. 日本語の法的概念を、1文字ではなく意味のある語・複合語として照合する。
2. 「敷金返還」と「敷金の残額を返還しなければならない」のような表現差を取得できる。
3. 未知語、法令固有語、送り仮名差を理由に候補を失わない。
4. 特定法令内検索でも、字面・意味・フレーズの複数経路を利用する。
5. 法令は既存のArticle単位統合を再利用し、chunkの多いArticleによる候補枠独占を防ぐ。
6. 現行索引を破壊せず、同じ質問で旧新を比較できる。
7. 質問別の条番号、正解語、手作り同義語を検索ロジックへ埋め込まない。

## 4. 非目標

- 検索スコアだけで法的結論や適用関係を確定すること
- LLMへ法的役割や探索順を固定的に指示すること
- 評価12問の正解条文を優遇すること
- Graphの推測エッジを増やすこと
- Analyzer変更と同時に回答生成方式まで切り替えること

## 5. 提案する索引構成

### 5.1 基本方針

同じ文字列を複数のmulti-fieldへ索引し、それぞれの長所を使う。

| 経路 | 目的 | 初期位置づけ |
|---|---|---|
| 日本語形態素解析 | 法的概念・複合語の通常検索 | 主 |
| exact phrase | 「敷金」「正当事由」等の連続一致 | 強い加点 |
| 2文字または2〜3文字N-gram | 未知語・表記差・辞書外語の回収、無関係候補の足切り | 比較対象 |
| ベクトル | 自然言語と法令表現の意味差を回収 | 独立経路 |
| keyword / 数値フィールド | documentId・Article番号等の直接指定 | 決定的 |

1文字N-gramは主検索には使わない。必要性が確認された場合も、低重みの診断用経路として
別管理する。

N-gramの位置づけは、実装前に「弱い補助」と確定しない。日本語全文検索の一般例では、
N-gramを`must`に置いて再現率の床兼ノイズの足切りとし、形態素解析を`should`で加点する構成も
使われる。一方、本件の既知障害は「必要Articleが全く検索できない」より「41件中25位になる」
順位問題であり、N-gram必須化が言い換え検索のrecallを落とす可能性もある。Phase 0では次の
2系統を同じデータで比較する。

1. **morphology-primary**: 形態素・phraseを主経路、N-gramを`should`または独立した低寄与laneにする。
2. **ngram-floor**: N-gram phraseを`must`、形態素・phrase・vectorを順位向上に使う。

### 5.2 形態素解析

第一候補は`analysis-kuromoji`である。`kuromoji_tokenizer`を用い、「敷金」「賃貸人」
「返還義務」等を語として扱う。

代替候補:

- OpenSearch組み込みの`cjk` Analyzer
- `analysis-icu`の`icu_analyzer`
- Works Applicationsの`analysis-sudachi`

Kuromojiは日本語の語境界を扱いやすいが、プラグイン導入とOpenSearchバージョン一致が必要で
ある。組み込みCJKは導入が容易だが、基本的にbigram中心であり、法的複合語の意味単位とは
一致しない場合がある。

SudachiはOpenSearch 2.6〜2.19対応ブランチを持ち、`split_mode=A|B|C`により最短・中間・
固有表現寄りの単位を使い分けられる。「原状回復」「返還義務」のような法的複合語をCで保ち、
AまたはBで構成語も拾う設計が可能である。ただしOpenSearch公式プラグインではなく、
2.19.1用のビルド、辞書配置、イメージサイズ、ライセンス、arm64/amd64の再現性を確認する必要が
ある。そのためKuromojiを基準候補、SudachiをPhase 0のchallengerとし、採用を前提にしない。

### 5.3 N-gram

N-gramは、`min_gram=2, max_gram=2`のbigramと、`min_gram=2, max_gram=3`を比較する。
1文字一致によるノイズを避けつつ、「製造販売」「譲渡制限」「有価証券」のような辞書外複合語も
部分一致させる。

bigramは日本語全文検索の一般的な起点だが、2〜3文字構成よりtoken数と候補が増え得る。
`ngram-floor`ではphraseと`minimum_should_match`を使って候補を絞り、
`morphology-primary`では独立laneとして融合する。query側もN-gram化するかは
`search_analyzer`と一体で比較し、候補数だけで判断しない。

### 5.4 provisional mapping

次は議論用の概念例であり、そのまま投入する確定JSONではない。特に
`discard_compound_token`の異なる2つのtokenizerを分けている。

```json
{
  "settings": {
    "analysis": {
      "char_filter": {
        "ja_nfkc": {
          "type": "icu_normalizer",
          "name": "nfkc",
          "mode": "compose"
        }
      },
      "tokenizer": {
        "ja_kuromoji_keep_compound": {
          "type": "kuromoji_tokenizer",
          "mode": "search",
          "discard_compound_token": false
        },
        "ja_kuromoji_phrase": {
          "type": "kuromoji_tokenizer",
          "mode": "search",
          "discard_compound_token": true
        },
        "ja_bigram_tokenizer": {
          "type": "ngram",
          "min_gram": 2,
          "max_gram": 2,
          "token_chars": ["letter", "digit"]
        }
      },
      "analyzer": {
        "ja_morph": {
          "type": "custom",
          "char_filter": ["ja_nfkc"],
          "tokenizer": "ja_kuromoji_keep_compound",
          "filter": ["kuromoji_baseform", "cjk_width", "lowercase"]
        },
        "ja_phrase": {
          "type": "custom",
          "char_filter": ["ja_nfkc"],
          "tokenizer": "ja_kuromoji_phrase",
          "filter": ["kuromoji_baseform", "cjk_width", "lowercase"]
        },
        "ja_bigram": {
          "type": "custom",
          "char_filter": ["ja_nfkc"],
          "tokenizer": "ja_bigram_tokenizer",
          "filter": ["lowercase"]
        }
      }
    }
  },
  "mappings": {
    "properties": {
      "heading": {
        "type": "text",
        "analyzer": "ja_morph",
        "fields": {
          "keyword": {"type": "keyword"},
          "phrase": {"type": "text", "analyzer": "ja_phrase"},
          "ngram": {"type": "text", "analyzer": "ja_bigram"}
        }
      },
      "text": {
        "type": "text",
        "analyzer": "ja_morph",
        "fields": {
          "phrase": {"type": "text", "analyzer": "ja_phrase"},
          "ngram": {"type": "text", "analyzer": "ja_bigram"}
        }
      }
    }
  }
}
```

Phase 1の成果物は、この断片を単独で使うのではなく、現行
`opensearch_index_mapping.sample.json`全体から派生させる。少なくとも次を維持する。

- `settings.index.knn=true`
- `embedding`の`knn_vector`定義とdimension
- `documentId`、`contentUnitId`、`articleContentUnitId`、`docType`、`sectionKey`等のkeyword
- authority、clearance、publish status、source metadata等の既存field

この全体継承をmappingの自動テストで固定し、新Analyzer追加によってfilterやvector検索を
失わないことを確認する。

検討事項:

- `search_analyzer`をindex Analyzerと分けるか
- Kuromojiの`mode=search|normal|extended`と
  `discard_compound_token=true|false`の組合せ
- BM25用は複合語を保持し、phrase用は複合語を捨てる分離が実測上も必要か
- `kuromoji_baseform`、`kuromoji_number`、品詞除去、`ja_stop`、記号正規化の要否
- 法令番号、全角数字、漢数字、期間表現の正規化をAnalyzerで行うか既存パーサーへ任せるか
- `title`と`sectionPath`にも同じmulti-fieldを持たせるか

`mode=search`は長い名詞を分割しつつ元の複合語も同じ位置へ出す。
`discard_compound_token=true`は元の複合語を捨てるため、phraseの位置関係を単純にできる一方、
BM25で「敷金」等の複合語そのものを失う可能性がある。このため同じAnalyzerを全laneへ流用しない。
ただし、`match_phrase`を使うだけで常に`true`が必要とは決めない。一次資料で明示されている
主な用途は複合tokenが後段の同義語展開を妨げる問題の回避である。phrase検索については
`_analyze`のposition / positionLengthと実検索結果を比較して決める。

`kuromoji_part_of_speech`や`ja_stop`を無条件に使わない。法令の「及び／並びに／又は／若しくは」は
適用関係を区別するため、少なくともphrase fieldでは品詞・stopword除去をしない。
`kuromoji_number`も期間や条番号を変換するため、概念語fieldへ即採用せず、数値診断を通す。

同義語は初期実装に入れない。一般同義語を導入する場合もsearch Analyzer側の
`synonym_graph`だけで比較し、評価12問固有の語を登録しない。index時同義語により辞書変更のたびに
再索引が必要になる構成を避ける。

## 6. 提案する検索フロー

### 6.1 優先順位

```text
質問またはLLMが作った検索語
  │
  ├─ A. Article ID・条番号の直接取得
  ├─ B. Kuromoji BM25
  ├─ C. exact phrase
  ├─ D. 2文字または2〜3文字N-gram
  └─ E. ベクトル検索
           ↓
       B〜Eをchunk単位でRRF等の順位融合
       Aは決定的な直接取得として別に統合
           ↓
       法令=Article / ガイド=chunk単位へ変換
           ↓
       上位法令Article / ガイドchunkをLLMへ提示
            └─ Cross-Encoderは別の導入判断
```

直接Article取得、BM25、documentId filter付きvector、chunk単位RRF、法令Article集約、
ガイドchunk返却は`search_requirement_specs()`に実装済みである。この経路を共通基盤とし、
phrase / N-gram laneだけを追加する。

直接取得できるArticle IDは既存の`_direct_article_query()`と`direct`得点を再利用する。ただし
LLMが記憶から生成した未確認IDは利用せず、検索結果・文書索引・Graphで確認済みのIDだけ許可する。

OpenSearch 2.19はsearch pipelineの`score-ranker-processor`としてRRFを持つため、既存の
アプリ側RRFだけを唯一の実装候補とはしない。ただし2.19で導入されたRRFには次の制約がある。

- hybrid queryのサブクエリは最大5件である。
- `rank_constant`の既定は60である。
- 2.19導入時のRRFはlane別weightを対象外としている。
- 順位融合なので、直接Article取得を「常に最優先」にする意味は表現できない。
- filterは各サブクエリへ入れ、集約やページング制約も個別に検証する必要がある。

したがってnative hybridの対象はB〜Eの最大4laneに限定し、Aは従来どおりアプリ側で先に保護する。
Phase 0では、同じlane・同じ候補深度を使って次を比較する。

1. 既存アプリ側RRF（direct優先、lane weightを維持可能）
2. OpenSearch native RRF（`rank_constant=60`を基準、40も独立データで比較）
3. OpenSearch normalization processor（weightが必要な場合の比較候補）

native化は「正確さ・trace・時間」が改善した場合だけ採用する。既存処理は既に`_msearch`で
batchされているため、native化だけで外部呼び出し数が必ず減るとは仮定しない。

### 6.2 字面検索

初期案:

- `match_phrase`を強く加点
- `text`の形態素BM25を主得点にする
- `heading`は本文より少し高くするが、現行の固定`heading^8`は再検証する
- N-gramを`should`へ置く案と、phraseの`must`へ置く案を比較する
- 複数語の最低一致率を設定する場合、法令本文の言い換えを落とさないようshadowで測る

特定の法令内では`documentId`をfilterし、法令名の一致をスコアへ混ぜない。
現行`_requirement_query()`はdocumentId確定時も`title`を検索fieldへ含めるため、これは明示的な
修正対象である。titleを除外した場合と低boostで残した場合を比較する。

N-gram fieldをquery時にもN-gram化すると候補が増えすぎる可能性がある。index Analyzerと
search Analyzerの組合せ、および`minimum_should_match`を一体で比較する。

形態素・N-gramのいずれも`type=phrase`を使う案を含める。ただし自然言語の語順と法令本文の
語順は一致しないため、phrase不一致を全lane共通の除外条件にはしない。

### 6.3 ベクトル検索

`search_requirement_specs()`の`_requirement_vector_query()`は、KNNのfilterへdocumentId /
docType / authority / clearance等を設定できる。新規実装せず、LLM主導経路をこのAPIへ接続する。

ベクトルは意味の近さを補うが、条番号、人数、期間、例外等の精密な字面を保証しない。
字面検索と別経路で候補を取得し、片方だけで十分としない。

### 6.4 法令Articleとガイドchunkの候補単位

候補単位はdocTypeで異なる。

- 法令: Article単位
- ガイド: chunk/page単位

ガイドには`articleContentUnitId`がなく、headingは`p.N`等のページ単位である。既存実装も
`_articles_from_hits()`と`_chunks_from_hits()`を`spec.doc_type`で切り替えている。この分岐を
維持する。

法令についてはOpenSearchのchunk hitを次のように既に統合している。

1. `articleContentUnitId`、`parentContentUnitId`、`contentUnitId`からArticle IDを求める。
2. 同一Articleの複数項・号を1候補へ統合する。
3. Articleの代表スコアと、経路別順位を保持する。
4. Article確定後に、質問と関係する項・号を回答コンテキスト用に選ぶ。

これにより、長い条文の複数chunkが候補上限を消費することを防ぐ。

未決事項はArticle代表スコアである。現行はchunkスコアの`max`を採用するため、1chunkが強く
一致するArticleを優先できる一方、複数項が中程度に一致するArticleを過小評価し得る。
`max`、上位N件平均、`max + capped support`等をretrieval-onlyで比較する。代表chunkは
スコア降順の先頭を初期値とする。

### 6.5 候補上限

上限を先に固定せず、Phase 0で次を測定して決める。

- Kuromoji上位Article数
- phrase上位Article数
- N-gram上位Article数
- vector上位Article数
- 重複排除後Article数
- LLMへ提示するArticle/chunk数
- embedding、OpenSearch、LLMの各所要時間

候補を増やすだけでは精度を保証できない。必要Article recallとノイズ率、OpenSearch・embedding・
LLM時間を同時に測る。

LLM主導経路には現在Cross-Encoderがない。導入する場合は別実験とし、Analyzer改善の必須条件に
しない。導入実験時だけペア数と所要時間を追加計測する。

## 7. LLM主導方式との接続

LLMは引き続き次を判断する。

- 何を検索するか
- どの法令・ガイドへ絞るか
- 追加検索が必要か
- 取得した証拠で回答できるか
- 回答に使うcontentUnit

検索バックエンドは、LLMが指定した`query`と`documentIds`に対し、上記の複数検索経路を
機械的に実行する。法的役割、必要条文、検索順をバックエンドへ固定しない。

同一LLMターンの`search_corpus` actionsは1件ずつ逐次実行せず、docType別
`RequirementSearchSpec`へ変換して1回の`search_requirement_specs()`へbatchする。
論理tool call数はaction数としてtraceへ残し、OpenSearch外部呼び出し数は別に記録する。

初期の最大逐次経路は次とする。

| component | requestあたりの最大呼び出し |
|---|---:|
| document catalog | 1 |
| LLM decision | `LLM_RESEARCH_MAX_TURNS`（既定3） |
| embedding batch | 最大3（各LLMターン1回） |
| OpenSearch `_msearch` | 最大3（各LLMターン1回） |
| Article直接取得 / Graph | LLM actionに応じるが共有tool上限内 |

時間表はこの回数を掛けたrequest全体で作る。Cross-Encoder時間は含めない。

必要であれば、`search_corpus`に次の任意入力を追加する案を比較する。

- `exactPhrases`
- `mustTerms`
- `excludeTerms`

ただし必須にはしない。LLMが単一の自然言語queryしか返さなくても、バックエンドの標準検索で
一定の候補品質を確保する。

## 8. 法令とガイドの文書内検索

共通の文書内検索を新設せず、既存`search_requirement_specs()`を再利用する。
LLMの`ResearchAction`から次のように`RequirementSearchSpec`へ変換する薄いadapterを
`llm_research_tools.py`に置く。

```text
ResearchAction.query       → spec.query
ResearchAction.documentIds → spec.document_ids
登録済みdocument metadata  → spec.doc_type
設定上限                    → spec.top_k
```

`doc_type`はLLMの推測値をそのまま信用せず、登録済みdocument metadataから解決する。
複数documentIdに法令とガイドが混在する場合はspecをdocType別に分ける。同じ`_msearch`
requestへまとめても、結果は法令Articleとガイドchunkに分けて保持する。

`search_requirement_specs()`の戻り値はLLM主導loopの`EvidenceCatalog`と形が異なるため、
adapterで次を明示的に変換する。

- 法令候補: Article順位を保持し、候補の`chunks`から代表content unitを登録する。
- ガイド候補: `source`のchunkをそのまま登録する。
- Article内の複数chunkを何件提示するかは共有コンテキスト上限で制限する。
- 検索順位・lane・Article IDをtrace metadataとして失わない。

旧`search_by_document_id()`は互換経路として残せるが、LLM主導方式からは呼ばない。

## 9. OpenSearchイメージと再シード

### 9.1 プラグイン

現行Composeは`opensearchproject/opensearch:2.19.1`を直接使用している。k-NNとNeural Searchは
通常版OpenSearch distributionにbundleされるため、native hybridのためだけの派生イメージは
不要である。一方、上記のAnalyzer候補には追加pluginが必要になる。

- Kuromoji: `analysis-kuromoji`
- NFKCの`icu_normalizer`: `analysis-icu`
- Sudachi: サードパーティ`analysis-sudachi`とSudachi辞書

KuromojiとICUを採用する場合は、同じOpenSearchバージョンのpluginをインストールした
派生イメージを作る。

概念例:

```dockerfile
FROM opensearchproject/opensearch:2.19.1
RUN /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-kuromoji \
 && /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-icu
```

起動時インストールは再現性と起動時間に影響するため避ける。プラグイン一覧とAnalyzerの
`_analyze`結果をhealthまたは検証スクリプトで確認する。

Sudachiは同じイメージへ同時に載せる前提にしない。Phase 0用の別イメージで
OpenSearch 2.19.1向けpluginをbuildし、core/fullのどの辞書を使ったか、辞書checksum、
plugin versionを記録する。Kuromoji/CJKと比較して明確なretrieval改善がない限り、
運用依存の小さい公式Kuromojiを優先する。

### 9.2 別索引での比較

Analyzerは既存tokenを変更できないため、再索引が必要である。現行索引を直接削除せず、
別索引を作る。

```text
legal-rag-content-v1  現行
legal-rag-content-v2  新Analyzer
```

shadow比較段階ではaliasを導入しない。`OPENSEARCH_INDEX`へ
`legal-rag-content-v1`または`legal-rag-content-v2`という**具象index名**を設定し、それぞれを
独立にseed・起動する。rollbackは環境変数をv1へ戻す。

現行`recreate_index()`は`DELETE /{index}`の応答を検証せず、続けて`PUT /{index}`する。
`OPENSEARCH_INDEX`へalias名を設定すると、DELETE/PUTが期待どおり動かずseed自体が失敗する。
したがってPhase 1〜3ではaliasを使わない。

aliasはPhase 4の切替候補とする。その前に次を実装・テストする。

- 対象名がaliasか具象indexかの判定
- write indexの解決
- aliasをDELETEしない保護
- DELETE応答の`raise_for_status()`
- alias切替の原子的操作

この保護がない状態ではaliasをseed先へ指定しない。

## 10. Trace

検索ごとに次を残す。

- analyzer/search lane名
- index/search Analyzer名、主要token filter、`discard_compound_token`
- query
- documentId / docType filter
- lane別chunk hit数
- lane別Article数
- Article IDとlane別順位・スコア
- 統合方式（app RRF / native RRF / normalization）、`rank_constant`、weight、統合後順位
- 必要Articleが初めて現れたlaneと順位（評価時のみ事後計算）
- embedding、OpenSearch、LLMの各所要時間と呼び出し回数
- contextへ採用したcontentUnitId
- `_explain`によるfield別・term別寄与、IDF、文書長（診断queryのみ）

Cross-Encoderを別実験で導入する場合だけ、入力ペア数・出力順位・所要時間を追加する。

異なるlaneのBM25・ベクトル値を直接比較せず、RRF等の順位融合または正規化方法を明示する。

## 11. 評価計画

### 11.1 評価データ

1. Analyzer単体用の一般的な法令検索語
2. 登録済み自然言語12問
3. 高難度自然言語質問
4. lawqa_jp 140問
5. 既知問題を除いた集計

12問の正解条文は評価後の照合だけに使い、Analyzerのユーザー辞書、同義語、boostへ入れない。

### 11.2 指標

主指標:

- 必要Article recall@8 / @16 / @30
- 必要Article完全到達率
- LLMが最終選択した必要Article再現率

補助指標:

- MRR
- lane別の必要Article到達数
- Article候補数
- retrieval-only p50 / p90
- LLM主導方式の`ready`率
- timeout率、接続障害率、形式エラー率
- OpenSearch索引サイズ
- seed時間
- LLM主導1requestあたりのembedding / OpenSearch / LLM呼び出し回数
- app RRF / native RRF / normalization別のrecall・MRR・時間
- Analyzer構成別の索引token数と候補数

`recall@30`は`LLM_RESEARCH_SEARCH_TOP_K`の許容上限30で測定可能である。Cross-Encoderを
別実験で有効にした場合だけ、そのペア数を補助指標へ追加する。

### 11.3 診断ケース

少なくとも次を固定した診断にする。

| 検索対象 | 検索語 | 確認事項 |
|---|---|---|
| 民法 | 敷金 返還 原状回復 賃貸借終了 | 622条の2がどのlane・何位か |
| 民法 | 敷金 | 622条の2が上位を維持するか |
| 借地借家法 | 建物所有目的 土地賃貸借 存続期間 | 3条が取得できるか |
| 民法 | 賃借権 譲渡 転貸 承諾 | 612条が取得できるか |
| 民法 | 賃貸借 存続期間 五十年 50年 ５０年 | 604条、漢数字・半角・全角の正規化差 |
| 任意の法令本文 | A及びB / A又はB / A若しくはB | phrase fieldが並列関係語を落とさないか |

これは質問別の優遇ルールではなく、変更前後の失敗再現テストである。

### 11.4 比較方法

同じquery・filterをv1とv2へ送り、保存した順位を比較する。現行
`OpenSearchClient`はindex名を1つしか持たないため、初期段階では具象index名の異なる2つの
service/container profileで同じretrieval-only入力を実行する。同一process内比較を行うためだけに
client APIを拡張しない。LLMの揺らぎを混ぜないretrieval-only比較を先に行い、改善した場合だけ
LLM主導3問・12問を実行する。

段階:

1. Analyzerと字面検索だけ比較
2. 字面＋ベクトル融合を比較
3. Article統合後を比較
4. LLM主導loopを比較
5. 必要性が示された場合だけCross-Encoder有無を比較

1〜3では、少なくとも次の実験軸を分離する。

- morphology-primary / ngram-floor
- bigram / 2〜3gram
- Kuromojiの複合語保持 / phrase用複合語破棄
- Kuromoji / CJK / Sudachi A・B・C
- app RRF / native RRF（`rank_constant=60|40`）/ normalization

全組合せの総当たりは行わない。まず`_analyze`と小規模retrieval-onlyで明らかに不適切な構成を
落とし、その後に同じ候補深度・同じ評価入力で融合方式を比較する。

## 12. 初期合格条件案

Phase 0で旧方式の分母とbaselineを確定してから数値を固定する。着手前の原則は次とする。

1. 既知の622条の2事例が、融合前または融合後の上位8 Articleへ入る。
2. 自然言語12問の必要Article完全到達率が旧方式を下回らない。
3. lawqa_jpの必要Article recallが旧方式を下回らない。
4. 既知問題を除いた集計も別に満たす。
5. retrieval-only p90が運用時間予算内である。
6. LLM・回答生成を含むtimeout率を悪化させない。
7. 索引サイズとseed時間の増加を記録し、許容値を決定する。
8. ガイドのdocumentId指定検索が0件固定にならない。

12問だけの改善をもって採用しない。

## 13. 実装フェーズ案

### Phase 0: ベースラインとAnalyzer spike

- v1で診断queryの全順位を保存する。
- `_explain`でfield別・term別寄与、IDF、文書長を保存する。
- Kuromoji、CJK、bigram、2〜3文字N-gram、Sudachi A/B/Cの`_analyze`出力を比較する。
- Kuromojiは`discard_compound_token=true|false`、主要filter有無を比較する。
- NFKC、原形化、漢数字、全角・半角、並列関係語のtoken出力を保存する。
- morphology-primaryとngram-floorの候補数・recall・MRRを比較する。
- app RRFとnative RRF（`rank_constant=60|40`）、必要ならnormalization processorを比較する。
- 代表的な法令本文で索引サイズと検索時間を測る。
- Kuromoji・ICUプラグインのarm64/amd64環境での起動を確認する。
- Sudachiは別イメージで2.19.1対応build、辞書配置、ライセンス、起動を確認する。
- Analyzerの採用判断には登録済み12問を使わず、一般的な診断語と独立例を使う。
- 自然言語12問・lawqa_jpは採用候補確定後のbaseline/回帰比較にのみ使う。
- 現行`search_requirement_specs()`のlane、batch、Article/guide分岐を契約テストで固定する。

完了条件:

- 主AnalyzerとN-gramの役割、複合語保持、filter chainを1案に絞れる。
- Sudachiを採用するか、Kuromojiへ戻すかを再現可能な計測で決められる。
- app/nativeのどちらで融合するかを決められる。
- 候補上限と時間の暫定値を決められる。

### Phase 1: v2索引

- OpenSearch派生イメージを追加する。
- 現行sample mapping全体から派生したv2 mappingを作成する。
- KNN、embedding、keyword filter、metadataが全て残ることをテストする。
- `OPENSEARCH_INDEX`へ具象v1/v2名を指定し、v1を削除しないseed手順を追加する。
- このPhaseではaliasを導入しない。
- Analyzerとdocument metadataのhealth確認を追加する。

### Phase 2: 共通検索経路の拡張

- `llm_research_tools._search()`を`search_requirement_specs()`へ接続する。
- 既存BM25 / vector / direct / `_msearch` / RRFを再利用する。
- phrase / morphology / N-gram laneを`search_requirement_specs()`へ追加する。
- Phase 0で採用したapp RRFまたはOpenSearch search pipelineを実装する。
- 法令=Article、ガイド=chunk/pageの既存分岐を維持する。
- documentId確定時の`title` field、`heading^8`、`minimum_should_match`を比較する。
- `SUPPLEMENTARY_PROVISION_CUES`による附則除外の維持・撤去・明示指定を比較する。
- Article代表スコア`max`と代替集約を比較する。
- lane別traceを追加する。
- 現行検索はflagで維持する。

### Phase 3: shadow評価

- 同じqueryをv1/v2へ実行する。
- retrieval-onlyで12問・140問を比較する。
- 合格した場合だけLLM主導3問、次に12問を実行する。

### Phase 4: 切替

- 最初は`OPENSEARCH_INDEX`またはfeature flagでv2をactiveにする。
- aliasを採用する場合は、alias判定・write index解決・DELETE保護・原子的切替を先に実装する。
- 現在の`legal-rag-content`は具象index名なので、同名aliasはそのまま作れない。既存具象indexの
  退避・削除手順を検証するか、衝突しない新alias名を採用する。
- v1とrollback手順を一定期間維持する。
- 評価結果と確定設定をRUNBOOKへ記録する。

## 14. リスク

### 14.1 N-gramの索引肥大

bigramと2〜3文字N-gramはtoken数を増やす。全法令本文へ無条件に追加すると索引サイズ・
seed時間・BM25候補数が増える。Phase 0で増加率を測り、必要ならheadingとtextで構成を分ける。

### 14.2 Kuromoji辞書と法令固有語

形態素解析は未知の複合語を望ましくない位置で分割する可能性がある。Kuromojiでは
`discard_compound_token`、SudachiではA/B/Cの選択が順位とphrase位置へ影響する。N-gramと
ベクトルを補助にする。12問固有のユーザー辞書を作ることは禁止する。一般的な法令用語辞書を
導入する場合も、独立データで効果を確認する。

### 14.3 phrase偏重

自然言語と条文は同じ表現にならないことが多い。phraseは強い加点に使うが、phrase不一致を
除外条件にしない。

### 14.4 候補増加による精度・時間悪化

laneを増やすと候補も増える。各laneの候補数とrequest全体のArticle上限を別に持ち、
LLMへ提示するArticle/chunk数を共有予算で制限する。LLM主導経路の初期時間表は
`embedding + OpenSearch + LLM`で作り、現状存在しないCross-Encoder時間を含めない。

### 14.5 スコアの混在

BM25、phrase、N-gram、ベクトルの値は尺度が違う。生スコアの単純加算は避け、RRFまたは
検証済みの正規化を使う。

### 14.6 再シードの破壊性

現行seedは索引を再作成する。Phase 1〜3は具象v1/v2名だけを使い、aliasをseed先にしない。
Phase 4でaliasを採用する場合だけ、alias保護を実装する。

### 14.7 法令語を失うfilter chain

一般的な日本語検索で有効な品詞除去、stopword除去、数値正規化でも、法令では意味を変え得る。
特に「及び／又は／若しくは」や期間・条番号を落としたり同一視したりしないことを、
phrase laneと数値診断で固定する。filterを増やすこと自体を改善とみなさない。

### 14.8 サードパーティplugin

Sudachiは分割粒度の利点がある一方、OpenSearch本体と別のrelease、辞書配置、イメージ肥大、
脆弱性対応が必要になる。retrieval精度の小さな差だけで採用せず、build再現性と運用負荷を
合格条件へ含める。

### 14.9 native hybridのバージョン差

OpenSearch最新ドキュメントの機能を2.19.1へ誤適用しない。search pipelineの設定は
2.19.1実機で作成・検索する契約テストを持ち、サブクエリ上限、weight、filter、paginationを
固定する。OpenSearchを更新する場合は、融合仕様を再評価する。

## 15. 確定事項と残るレビュー論点

コードレビューと日本語検索の一次資料確認を受け、次を確定事項とする。

1. 第3の検索APIを新設せず、`search_requirement_specs()`を共通基盤にする。
2. documentId filter付きvector、direct、RRF、Article集約、guide分岐、`_msearch`は再利用する。
3. phrase / morphology / N-gramだけを既存lane構造へ追加する。
4. phrase・形態素・N-gram・vectorは生スコア加算ではなくlane別順位融合を基本とする。
5. shadow比較は具象v1/v2 index名で行い、Phase 1〜3ではaliasを使わない。
6. 法令候補単位はArticle、ガイド候補単位はchunk/pageとする。
7. LLM主導経路ではCross-Encoderを初期前提にしない。
8. CJK、Kuromoji、SudachiはPhase 0の`_analyze` spikeで比較し、Sudachiは別イメージの
   challengerとする。
9. N-gramを「弱い補助」と先に固定せず、morphology-primaryとngram-floorを比較する。
10. BM25用とphrase用でKuromojiの複合語保持を分けて比較する。
11. 品詞・stopword除去と数値正規化を法令本文へ無条件に適用しない。
12. OpenSearch native RRFはB〜Eの比較候補にし、直接Article取得はアプリ側で保護する。

他のAI・実装者には、残る次の点を確認してもらう。

1. morphology-primaryとngram-floorのどちらが法令Article recallと順位を両立するか。
2. index Analyzerとsearch Analyzer、`minimum_should_match`の比較条件は十分か。
3. bigramと2〜3gram、query Analyzerの組合せが候補を増やしすぎないか。
4. provisional mapping全体版にOpenSearch 2.19.1で無効な設定がないか。
5. documentId確定時に`title`を除く判断とheading boostの比較方法は妥当か。
6. Article代表スコアは`max`を維持すべきか、複数chunk一致を加味すべきか。
7. 附則除外をquery cueで決める現行ヒューリスティックをどう扱うべきか。
8. `ResearchAction`からdocType別`RequirementSearchSpec`への変換に抜けがないか。
9. 具象v1/v2 indexのseed・比較手順にデータ消失リスクがないか。
10. Phase 4でaliasを導入する価値が、環境変数切替より大きいか。
11. 自然言語12問をAnalyzer選定に使わない分離が十分か。
12. `embedding + OpenSearch + LLM`の呼び出し回数・時間予算が成立するか。
13. Sudachiの精度差がサードパーティpluginの運用負荷に見合うか。
14. app RRF / native RRF / normalizationの比較が同じlane・候補深度で行われるか。
15. phrase fieldが複合語位置と法令の並列関係語を正しく保持するか。

## 16. 参照

- [OpenSearch Text analysis](https://docs.opensearch.org/latest/analyzers/)
- [OpenSearch CJK analyzer](https://docs.opensearch.org/latest/analyzers/language-analyzers/cjk/)
- [OpenSearch N-gram token filter](https://docs.opensearch.org/latest/analyzers/token-filters/ngram/)
- [OpenSearch multi-fields](https://docs.opensearch.org/latest/field-types/mapping-parameters/fields/)
- [OpenSearch search analyzer](https://docs.opensearch.org/latest/mappings/mapping-parameters/search-analyzer/)
- [OpenSearch plugin installation](https://docs.opensearch.org/latest/install-and-configure/plugins/)
- [OpenSearch additional plugins](https://docs.opensearch.org/latest/install-and-configure/additional-plugins/)
- [OpenSearch 2.19 score ranker processor](https://docs.opensearch.org/2.19/search-plugins/search-pipelines/score-ranker-processor/)
- [OpenSearch 2.19 hybrid query](https://docs.opensearch.org/2.19/query-dsl/compound/hybrid/)
- [OpenSearch 2.19 RRF design](https://github.com/opensearch-project/neural-search/issues/865)
- [Elasticsearch Kuromoji tokenizer](https://www.elastic.co/docs/reference/elasticsearch/plugins/analysis-kuromoji-tokenizer)
- [Elastic Japanese full-text search design](https://www.elastic.co/blog/how-to-implement-japanese-full-text-search-in-elasticsearch)
- [Works Applications analysis-sudachi](https://github.com/WorksApplications/elasticsearch-sudachi)
