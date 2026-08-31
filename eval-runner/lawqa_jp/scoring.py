"""lawqa_jp固有の正解ラベル採点。"""


def answer_accuracy(predicted_option_id: object, gold_option_id: object) -> int:
    """Datasetの正解ラベルとAgentが選んだ汎用候補IDを比較する。"""

    if predicted_option_id is None or gold_option_id is None:
        return 0
    return int(
        str(predicted_option_id).upper() == str(gold_option_id).upper()
    )
