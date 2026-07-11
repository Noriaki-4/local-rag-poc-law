# clearanceLevel / confidentiality 対応方針

## 1. 目的

POCで `confidentiality` と `clearanceLevel` の使い分けが揺れないように、最小対応表を定義する。

## 2. POC最小対応表

| confidentiality | 意味 | requiredClearanceLevel | 備考 |
|---|---|---:|---|
| public | 公開情報または公開法令 | 1 | lawqa_jp / e-Gov法令は原則これ |
| internal | 庁内・組織内資料 | 2 | 庁内マニュアル等の業務資料 |
| restricted | 限定共有資料 | 3 | POCでは原則未使用 |

## 3. フィルタ実装方針

検索時は以下を必須条件として注入する。

```text
publishStatus = published
isLatest = true
clearanceLevel <= user.clearanceLevel
```

`confidentiality` は表示・監査・補助判定に使う。実アクセス判定は `clearanceLevel` を主とする。

## 4. 登録時チェック

ドキュメント登録時に、`confidentiality` と `clearanceLevel` が対応表に反していないか検証する。

例:

```text
confidentiality = public     -> clearanceLevel must be 1
confidentiality = internal   -> clearanceLevel must be 2 or greater
confidentiality = restricted -> clearanceLevel must be 3 or greater
```

POCでは厳密なABAC/RBACまでは作らず、この対応表で統制する。
