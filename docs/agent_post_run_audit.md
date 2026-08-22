# Agent事後監査

## 目的

Agent処理の終了後に、「なぜその判断をしたか」を保存済み実行記録から説明する診断機能である。
通常の回答処理、CaseStore、検索結果、最終回答は変更しない。

これはLLM内部の思考過程を取得する機能ではない。判断時に明示された短い理由と実行記録を正本にし、
別のLLM呼出しで読み取り専用の事後説明を生成する。

```text
回答処理
  └─ SolverDecision.decision_reason
       └─ 構造検証に成功
            └─ decision_appliedとしてSnapshotへ保存
                 └─ 終了後、監査APIが説明
```

## 保存する情報

- そのStepで`continue`または`finalize`を選んだ短い理由
- Solverが実際に参照した`SolverContext`
- プログラムからLLMへ送ったPrompt・schemaと、それぞれのhash
- Promptを構成した外部ファイル、使用section、Profile version
- LLMからプログラムへ返った生payload、輸送検証結果、payload hash
- 構造検証を通過した`SolverDecision`
- 適用前後の状態
- 実行終了状態と最終回答

修復前の契約違反Decisionは`decision_applied`として扱わない。事後監査へ渡すときは
`SolverContext`を正本とし、同じEvidenceを前後のCaseStateから重複して渡さない。

双方向の境界は次の順で記録する。

```text
Program -> LLM: transport_input   (Prompt / schema / asset来歴)
LLM -> Program: transport_output  (生payload / 輸送検証)
正規化後:        solver_output     (SolverDecision)
構造検証失敗:    contract_violation
構造検証成功:    decision_applied  (適用前後のCaseState)
```

`promptHash / schemaHash / payloadHash / solverDecisionHash`は本文を保存しない`status`でも記録する。
完全な内容を比較する場合は`snapshot`を使う。同じhashは同じ記録内容を示すが、法的妥当性や契約合格を
意味しない。合否は`validationError`、`contract_violation`、`decision_applied`で判断する。

## 有効化

既定は無効であり、追加のLLM呼出しも診断ファイルも発生しない。事後監査する回答を実行する前に設定する。

```env
AGENT_FRAMEWORK_DIAGNOSTICS_MODE=snapshot
AGENT_FRAMEWORK_POST_RUN_AUDIT=on_demand
AGENT_FRAMEWORK_POST_RUN_AUDIT_MAX_TOKENS=2048
```

`status`には本文と完全な判断材料がないため、事後監査には使用できない。Docker環境では設定変更後に
`agent-api`を再作成する。

## 使用方法

まず`POST /answer/framework`を実行し、応答の`trace.agentFramework.caseId`を控える。
診断有効時は`appliedDecisionSequences`も返る。

最後に適用された判断を説明する。

```bash
curl -s http://localhost:8000/answer/framework/audit \
  -H 'content-type: application/json' \
  -d '{"caseId":"legal-...","inquiry":"なぜ完了と判断したのですか"}' \
  | jq
```

途中の判断を指定する場合は、既知のsequenceを追加する。

```json
{
  "caseId": "legal-...",
  "decisionSequence": 12,
  "inquiry": "なぜこの候補を取得対象にしたのですか"
}
```

## 応答の見方

| フィールド | 意味 |
|---|---|
| `recordedDecisionReason` | 判断時にSolver自身が記録した短い理由 |
| `explanation` | 保存記録に基づく事後説明 |
| `recordedFacts` | 記録に直接書かれている事実 |
| `inferences` | 複数の記録を組み合わせた事後的推論 |
| `sourceDecisionSequences` | 説明に使用した適用済みDecision |
| `limitations` | 記録不足等により説明できない事項 |

事後説明は元の内部推論の再現ではなく、もっともらしい後付け説明になる可能性がある。
判断時の`recordedDecisionReason`とID・状態記録を優先し、`inferences`は監査補助として扱う。

## モデルと保存先

監査には現在のIntegration Modelを使うため、同一provider内でGPT-4o miniやClaude Haikuへ切り替えられる。
モデル固有のThinking出力には依存しない。診断JSONLは
`EVAL_RESULTS_DIR/agent-framework-diagnostics/<case_id>.jsonl`へ保存され、`eval-results/`はGit管理外である。

Snapshotには質問、法令本文、Prompt等が含まれるため、通常ログへ転記せず、必要な診断実行だけで有効にする。
